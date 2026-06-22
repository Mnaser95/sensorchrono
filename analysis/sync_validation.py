"""Visual sync-validation video.

Generates sync_validation.mp4 for calibration and
experiment_validation.mp4 for the experiment. New recordings use exact
LSL-clock phase boundaries captured by the session FSM.
Layout per output frame:
  top   - camera footage scaled to output width
  below - one vertically stacked rolling strip for every recorded waveform
          modality (ECG/EMG/accelerometer, audio, and EEG)
Keyboard presses are marked with red vertical lines on the waveform strips
and a red border flash on the whole composite frame.

CLI
---
    python -m analysis.sync_validation recording.xdf --out-dir OUT/
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import wave
from pathlib import Path
from typing import Sequence

import numpy as np
import pyxdf


_WINDOW_S = 5.0      # seconds of waveform history shown per strip
_STRIP_H = 120       # pixel height of each waveform strip
_OUT_W = 1280        # output video width
_FLASH_DUR_S = 0.15  # how long the red-border flash lasts after a press
_BUFFER_S = 2.0      # seconds before first / after last press in the output


def _find_video(out_dir: Path) -> Path | None:
    for ext in ("mp4", "avi"):
        hits = sorted(out_dir.glob(f"*_video.{ext}"))
        if hits:
            return hits[0]
    return None


def _load_frames_csv(frames_csv: Path) -> tuple[np.ndarray, np.ndarray]:
    """Return (frame_idxs, lsl_times).
    Accepts both 't_read_lsl' (bridge output) and 'lsl_corrected_s' (postprocess output).
    """
    fidxs: list[int] = []
    ftimes: list[float] = []
    with open(frames_csv, newline="") as fh:
        for row in csv.DictReader(fh):
            fidxs.append(int(row["frame_idx"]))
            if "lsl_corrected_s" in row:
                ftimes.append(float(row["lsl_corrected_s"]))
            else:
                ftimes.append(float(row["t_read_lsl"]))
    return np.array(fidxs, dtype=np.int64), np.array(ftimes)


def _render_strip(
    times: np.ndarray,
    values: np.ndarray,
    t_now: float,
    window_s: float,
    press_ts: np.ndarray,
    width: int,
    height: int,
    sig_color: tuple[int, int, int],
    label: str = "",
) -> np.ndarray:
    """Render one rolling waveform strip as a BGR numpy image."""
    import cv2

    strip = np.full((height, width, 3), (20, 20, 20), dtype=np.uint8)
    t_start = t_now - window_s

    mask = (times >= t_start) & (times <= t_now)
    if mask.sum() > 1:
        seg_t = times[mask]
        seg_v = values[mask]
        v_min, v_max = float(seg_v.min()), float(seg_v.max())
        if v_max == v_min:
            v_max = v_min + 1.0
        x = ((seg_t - t_start) / window_s * (width - 1)).clip(0, width - 1).astype(np.int32)
        y_norm = (seg_v - v_min) / (v_max - v_min)
        y = (height - 1 - y_norm * (height - 6) - 3).clip(0, height - 1).astype(np.int32)
        pts = np.column_stack([x, y]).reshape(-1, 1, 2)
        cv2.polylines(strip, [pts], False, sig_color, 1, cv2.LINE_AA)

    # Keyboard press vertical markers
    for tp in press_ts:
        if t_start <= tp <= t_now:
            xp = int((tp - t_start) / window_s * (width - 1))
            cv2.line(strip, (xp, 0), (xp, height - 1), (60, 60, 230), 2)

    # "now" cursor at the right edge
    cv2.line(strip, (width - 1, 0), (width - 1, height - 1), (130, 130, 130), 1)

    if label:
        cv2.putText(strip, label, (6, height - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.40, (180, 180, 180), 1, cv2.LINE_AA)
    return strip


_WAVEFORM_BASES = (
    "ShimmerECG", "ShimmerEMG", "ShimmerAccel",
    "Audio", "EmotivEEG",
)
_TRACE_COLORS = (
    (255, 140, 60), (255, 144, 30), (80, 220, 120),
    (220, 120, 220), (80, 200, 240), (240, 180, 80),
)


def _base_stream_name(name: str) -> str | None:
    for base in _WAVEFORM_BASES:
        if name == base or name.startswith(f"{base}_"):
            return base
    return None


def _channel_label(stream: dict, index: int, fallback: str) -> str:
    try:
        channels = stream["info"]["desc"][0]["channels"][0]["channel"]
        return str(channels[index]["label"][0])
    except (KeyError, IndexError, TypeError):
        return fallback


def _build_waveform_traces(
    by_name: dict,
    ecg_corrected_ts,
    lag_ms: dict,
) -> list[dict]:
    """Build one representative, time-corrected trace per waveform stream."""
    traces: list[dict] = []
    for name in sorted(by_name):
        base = _base_stream_name(name)
        if base is None:
            continue
        stream = by_name[name]
        raw = np.asarray(stream.get("time_series", []))
        if raw.size == 0:
            continue
        if raw.ndim == 1:
            raw = raw[:, None]

        if name == "ShimmerECG" and ecg_corrected_ts is not None:
            times = np.asarray(ecg_corrected_ts, dtype=float)
        else:
            times = np.asarray(stream.get("time_stamps", []), dtype=float)
        if len(times) != len(raw) or len(times) < 2:
            continue

        lag = lag_ms.get(name)
        if lag is None:
            lag = lag_ms.get(base)
        if lag:
            times = times - float(lag) / 1000.0

        values: np.ndarray
        label: str
        if base == "Audio":
            values = raw[:, 0].astype(float)
            try:
                from scipy.signal import butter, filtfilt, hilbert
                fs = float(stream["info"]["nominal_srate"][0])
                lo, hi = 1000.0, min(6000.0, fs / 2.0 * 0.95)
                b, a = butter(4, [lo / (fs / 2.0), hi / (fs / 2.0)],
                              btype="band")
                values = np.abs(hilbert(filtfilt(b, a, values)))
                ds = max(1, int(fs / 1000.0))
                times, values = times[::ds], values[::ds]
            except Exception as exc:
                print(f"[sync_validation] {name} envelope failed: {exc}",
                      file=sys.stderr)
                values = np.abs(values)
            label = f"{name}: envelope (1-6 kHz)"
        elif base == "EmotivEEG":
            values = raw[:, 0].astype(float)
            try:
                from scipy.signal import butter, filtfilt
                fs = float(stream["info"]["nominal_srate"][0]) or 128.0
                nyquist = fs / 2.0
                b, a = butter(4, [1.0 / nyquist, 50.0 / nyquist],
                              btype="band")
                values = filtfilt(b, a, values)
            except Exception as exc:
                print(f"[sync_validation] {name} BPF failed: {exc}",
                      file=sys.stderr)
            channel = _channel_label(stream, 0, "channel 1")
            label = f"{name}: {channel} (1-50 Hz BPF)"

        else:
            # Shimmer streams carry device time in column 0; pick highest-variance signal column.
            if raw.shape[1] > 1:
                index = int(np.argmax(raw[:, 1:].std(axis=0))) + 1
            else:
                index = 0
            values = raw[:, index].astype(float)
            channel = _channel_label(stream, index, "channel 1")
            label = f"{name}: {channel}"
            if base in ("ShimmerECG", "ShimmerEMG"):
                try:
                    from scipy.signal import butter, filtfilt
                    fs = 1.0 / float(np.median(np.diff(times)))
                    b, a = butter(2, 1.0 / (fs / 2.0), btype="high")
                    values = filtfilt(b, a, values)
                    label += " (1 Hz HPF)"
                except Exception as exc:
                    print(f"[sync_validation] {name} HPF failed: {exc}",
                          file=sys.stderr)

        finite = np.isfinite(times) & np.isfinite(values)
        if finite.sum() < 2:
            continue
        traces.append({
            "name": name,
            "times": times[finite].astype(np.float64),
            "values": values[finite].astype(np.float32),
            "label": label,
            "color": _TRACE_COLORS[len(traces) % len(_TRACE_COLORS)],
        })
    return traces


def _stack_waveform_strips(
    camera_frame: np.ndarray,
    traces: list[dict],
    t_now: float,
    press_ts: np.ndarray,
) -> np.ndarray:
    """Place every waveform strip below the camera frame."""
    width = camera_frame.shape[1]
    strips = [
        _render_strip(
            trace["times"], trace["values"], t_now, _WINDOW_S,
            press_ts, width, _STRIP_H, trace["color"], trace["label"],
        )
        for trace in traces
    ]
    return np.vstack([camera_frame, *strips])


def generate_validation_video(
    by_name: dict,
    ecg_corrected_ts,
    lag_ms: dict,
    out_dir: Path,
    *,
    time_range: tuple[float, float] | None = None,
    output_name: str = "sync_validation.mp4",
    event_ts: np.ndarray | None = None,
) -> Path | None:
    """Render one synchronized validation video for the requested interval."""
    try:
        import cv2
    except ImportError:
        return None

    if "KeyboardFiducial" not in by_name:
        return None

    kb = by_name["KeyboardFiducial"]
    kb_ts = np.asarray(kb["time_stamps"], dtype=float)
    kb_ev = [v[0] for v in kb["time_series"]]
    all_press_ts = np.array(
        [t for t, e in zip(kb_ts, kb_ev) if "press" in e], dtype=float)
    press_ts = (all_press_ts if event_ts is None
                else np.asarray(event_ts, dtype=float))

    if time_range is None:
        if len(press_ts) == 0:
            return None
        range_start = float(press_ts[0]) - _BUFFER_S
        range_end = float(press_ts[-1]) + _BUFFER_S
    else:
        range_start, range_end = map(float, time_range)
        if range_end <= range_start:
            return None
        press_ts = press_ts[
            (press_ts >= range_start) & (press_ts <= range_end)]

    # Locate source video and frames CSV
    video_path = _find_video(out_dir)
    frames_csv = out_dir / "frames.csv"
    if video_path is None or not video_path.exists() or not frames_csv.exists():
        return None

    all_fidxs, all_ftimes = _load_frames_csv(frames_csv)
    video_lag_s = float(lag_ms.get("VideoFrames") or 0.0) / 1000.0
    all_ftimes_corrected = all_ftimes - video_lag_s
    interval_mask = (all_ftimes_corrected >= range_start) & (all_ftimes_corrected <= range_end)
    if interval_mask.sum() == 0:
        return None
    interval_fidxs = all_fidxs[interval_mask]
    interval_ftimes = all_ftimes_corrected[interval_mask]

    fidx_min = int(interval_fidxs[0])
    fidx_max = int(interval_fidxs[-1])

    # Pre-compute one vertically stacked strip for every waveform modality used.
    traces = _build_waveform_traces(by_name, ecg_corrected_ts, lag_ms)
    # --- Open source video ---
    cap = cv2.VideoCapture(str(video_path))
    ret, test_frame = cap.read()
    if not ret:
        cap.release()
        return None
    src_h, src_w = test_frame.shape[:2]
    fps_out = cap.get(cv2.CAP_PROP_FPS) or 30.0

    # Ensure even dimensions (codec requirement)
    out_w = _OUT_W if _OUT_W % 2 == 0 else _OUT_W - 1
    frame_h = int(out_w * src_h / src_w)
    if frame_h % 2 != 0:
        frame_h -= 1
    total_h = frame_h + _STRIP_H * len(traces)

    out_path = out_dir / output_name
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(out_path), fourcc, fps_out, (out_w, total_h))

    # --- Build output timeline at constant fps_out ---
    # Generate one output frame per 1/fps_out step over the full interval so
    # the output video has the correct wall-clock duration regardless of how
    # many source frames were dropped.  For each output time we pick the source
    # frame whose LSL timestamp is closest, then freeze on that frame until a
    # newer one is available (honest representation of dropped-frame gaps).
    n_out = max(1, int(round((range_end - range_start) * fps_out)))
    out_times = range_start + np.arange(n_out) / fps_out

    # Map every output time to the nearest entry in interval_ftimes.
    src_pos = np.searchsorted(interval_ftimes, out_times, side="left")
    src_pos = np.clip(src_pos, 0, len(interval_ftimes) - 1)
    for i in range(len(src_pos)):
        p = src_pos[i]
        if p > 0 and (abs(interval_ftimes[p - 1] - out_times[i])
                      < abs(interval_ftimes[p] - out_times[i])):
            src_pos[i] = p - 1
    target_fidxs = interval_fidxs[src_pos]

    # Read source frames sequentially (avoids costly random seeks).
    cap.set(cv2.CAP_PROP_POS_FRAMES, float(fidx_min))
    current_fidx = fidx_min
    cached_cam: np.ndarray | None = None
    frames_written = 0

    for t_now, want_fidx in zip(out_times, target_fidxs):
        # Advance the sequential reader up to the wanted frame.
        while current_fidx <= want_fidx:
            ret, frame = cap.read()
            if not ret:
                break
            if current_fidx == want_fidx:
                cached_cam = cv2.resize(frame, (out_w, frame_h))
            current_fidx += 1

        if cached_cam is None:
            continue

        composite = _stack_waveform_strips(cached_cam, traces, t_now, press_ts)
        # Red border flash on keyboard press
        if any(0.0 <= (t_now - tp) <= _FLASH_DUR_S for tp in press_ts):
            cv2.rectangle(composite, (0, 0),
                          (composite.shape[1] - 1, composite.shape[0] - 1),
                          (0, 0, 255), 10)

        # Elapsed time overlay
        cv2.putText(composite, f"t={t_now - range_start:.2f}s",
                    (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.75,
                    (255, 255, 255), 2, cv2.LINE_AA)

        writer.write(composite)
        frames_written += 1
        if frames_written % 150 == 0:
            print(f"[sync_validation] {frames_written} frames written...",
                  file=sys.stderr)

    writer.release()
    cap.release()
    print(f"[sync_validation] wrote {frames_written} frames -> {out_path.name}",
          file=sys.stderr)
    return out_path if frames_written > 0 else None


def _write_aligned_audio_segment(
    stream: dict,
    lag_ms: float | None,
    start_lsl: float,
    duration_s: float,
    output_path: Path,
) -> bool:
    """Write a WAV aligned to the validation video's corrected LSL timeline."""
    times = np.asarray(stream["time_stamps"], dtype=float)
    values = np.asarray(stream["time_series"])
    if values.ndim == 1:
        values = values[:, None]
    if len(times) == 0 or len(times) != len(values):
        return False

    fs = int(round(float(stream["info"]["nominal_srate"][0])))
    corrected = times - (float(lag_ms) / 1000.0 if lag_ms else 0.0)
    desired = max(1, int(round(duration_s * fs)))
    first = int(np.searchsorted(corrected, start_lsl, side="left"))
    last = int(np.searchsorted(
        corrected, start_lsl + duration_s, side="left"))
    first = min(first, len(corrected))
    last = min(max(last, first), len(corrected))

    with wave.open(str(output_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(fs)

        written = 0
        if first < len(corrected):
            leading = max(
                0, int(round((corrected[first] - start_lsl) * fs)))
            leading = min(leading, desired)
            wav.writeframes(np.zeros(leading, dtype=np.int16).tobytes())
            written += leading

        chunk_n = 1_000_000
        for offset in range(first, last, chunk_n):
            if written >= desired:
                break
            chunk = values[offset:min(last, offset + chunk_n), 0]
            count = min(len(chunk), desired - written)
            pcm = (np.clip(chunk[:count].astype(float), -1.0, 1.0)
                   * 32767.0).astype(np.int16)
            wav.writeframes(pcm.tobytes())
            written += count

        while written < desired:
            count = min(chunk_n, desired - written)
            wav.writeframes(np.zeros(count, dtype=np.int16).tobytes())
            written += count
    return True


def _mux_microphone_audio(
    video_path: Path,
    audio_stream: dict,
    lag_ms: float | None,
    start_lsl: float,
) -> bool:
    """Add synchronized microphone audio to an existing validation MP4."""
    import cv2
    from imageio_ffmpeg import get_ffmpeg_exe

    cap = cv2.VideoCapture(str(video_path))
    fps = float(cap.get(cv2.CAP_PROP_FPS))
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    if fps <= 0 or frames <= 0:
        return False
    duration_s = frames / fps

    wav_path = video_path.with_suffix(".audio.tmp.wav")
    muxed_path = video_path.with_name(f"{video_path.stem}.muxed.mp4")
    try:
        if not _write_aligned_audio_segment(
                audio_stream, lag_ms, start_lsl, duration_s, wav_path):
            return False
        command = [
            get_ffmpeg_exe(), "-y", "-loglevel", "error",
            "-i", str(video_path), "-i", str(wav_path),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-t", f"{duration_s:.6f}", str(muxed_path),
        ]
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=120)
        if result.returncode != 0 or not muxed_path.exists():
            print(f"[sync_validation] audio mux failed: {result.stderr[-500:]}",
                  file=sys.stderr)
            return False
        os.replace(muxed_path, video_path)
        return True
    finally:
        wav_path.unlink(missing_ok=True)
        muxed_path.unlink(missing_ok=True)


def generate_validation_videos(
    by_name: dict,
    ecg_corrected_ts,
    lag_ms: dict,
    out_dir: Path,
) -> list[Path]:
    """Generate calibration and experiment validation videos when boundaries exist."""
    phase_path = out_dir / "phase_boundaries.json"
    try:
        phase = json.loads(phase_path.read_text(encoding="utf-8"))
        experiment_start = float(phase["experiment_start_lsl"])
        experiment_end = float(phase["experiment_end_lsl"])
        calibration_start = float(
            phase.get("calibration_start_lsl", experiment_start))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        legacy = generate_validation_video(
            by_name, ecg_corrected_ts, lag_ms, out_dir)
        return [legacy] if legacy is not None else []

    kb = by_name.get("KeyboardFiducial")
    all_presses = np.array([], dtype=float)
    if kb is not None:
        kb_ts = np.asarray(kb["time_stamps"], dtype=float)
        kb_ev = [v[0] for v in kb["time_series"]]
        all_presses = np.array(
            [t for t, e in zip(kb_ts, kb_ev) if "press" in e], dtype=float)
    calibration_presses = all_presses[all_presses < experiment_start]

    if len(calibration_presses):
        calibration_range = (
            max(calibration_start,
                float(calibration_presses[0]) - _BUFFER_S),
            min(experiment_start,
                float(calibration_presses[-1]) + _BUFFER_S),
        )
    else:
        calibration_range = (calibration_start, experiment_start)

    requests = (
        (calibration_range, "sync_validation.mp4", calibration_presses),
        ((experiment_start, experiment_end),
         "experiment_validation.mp4", all_presses),
    )
    outputs: list[Path] = []
    for interval, name, events in requests:
        path = generate_validation_video(
            by_name, ecg_corrected_ts, lag_ms, out_dir,
            time_range=interval, output_name=name, event_ts=events,
        )
        if path is not None:
            if name == "experiment_validation.mp4" and "Audio" in by_name:
                _, frame_times = _load_frames_csv(out_dir / "frames.csv")
                in_interval = frame_times[
                    (frame_times >= interval[0])
                    & (frame_times <= interval[1])]
                if len(in_interval) == 0 or not _mux_microphone_audio(
                        path, by_name["Audio"], lag_ms.get("Audio"),
                        float(interval[0])):
                    raise RuntimeError(
                        "could not add microphone audio to experiment validation")
            outputs.append(path)
    return outputs


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate sync validation video from XDF.")
    ap.add_argument("xdf", type=Path)
    ap.add_argument("--out-dir", type=Path, required=True)
    args = ap.parse_args(argv)

    if not args.xdf.exists():
        print(f"ERROR: {args.xdf} not found", file=sys.stderr)
        return 2

    streams, _ = pyxdf.load_xdf(str(args.xdf), dejitter_timestamps=True,
                                 synchronize_clocks=True)
    by_name = {s["info"]["name"][0]: s for s in streams}
    args.out_dir.mkdir(parents=True, exist_ok=True)

    outputs = generate_validation_videos(
        by_name, None,
        {"ShimmerECG": None, "Audio": None, "VideoFrames": None},
        args.out_dir,
    )
    if outputs:
        for out in outputs:
            print(f"wrote {out}")
        return 0
    print("ERROR: could not generate video (missing video file, frames.csv, "
          "or KeyboardFiducial stream)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
