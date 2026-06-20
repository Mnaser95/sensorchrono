"""Visual sync-validation video.

Generates ``sync_validation.mp4`` covering the calibration period
(first to last keyboard press, ±2 s buffer).

Layout per output frame:
  top    — camera footage scaled to output width
  middle — rolling ECG strip (5-second history, orange)
  bottom — rolling audio envelope strip (1–6 kHz, 5-second history, green)

Keyboard presses are marked with red vertical lines on the waveform strips
and a red border flash on the whole composite frame.

CLI
---
    python -m analysis.sync_validation recording.xdf --out-dir OUT/
"""
from __future__ import annotations

import argparse
import csv
import sys
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


def generate_validation_video(
    by_name: dict,
    ecg_corrected_ts,
    lag_ms: dict,
    out_dir: Path,
) -> Path | None:
    """Generate sync_validation.mp4. Returns the output path or None if skipped."""
    try:
        import cv2
    except ImportError:
        return None

    if "KeyboardFiducial" not in by_name:
        return None

    kb = by_name["KeyboardFiducial"]
    kb_ts = np.asarray(kb["time_stamps"])
    kb_ev = [v[0] for v in kb["time_series"]]
    press_ts = np.array([t for t, e in zip(kb_ts, kb_ev) if "press" in e])
    if len(press_ts) == 0:
        return None

    cal_start = float(press_ts[0]) - _BUFFER_S
    cal_end = float(press_ts[-1]) + _BUFFER_S

    # Locate source video and frames CSV
    video_path = _find_video(out_dir)
    frames_csv = out_dir / "frames.csv"
    if video_path is None or not video_path.exists() or not frames_csv.exists():
        return None

    all_fidxs, all_ftimes = _load_frames_csv(frames_csv)
    cal_mask = (all_ftimes >= cal_start) & (all_ftimes <= cal_end)
    if cal_mask.sum() == 0:
        return None
    cal_fidxs = all_fidxs[cal_mask]
    cal_ftimes = all_ftimes[cal_mask]

    # fidx → LSL time lookup (used during sequential read)
    fidx_to_time: dict[int, float] = {
        int(f): t for f, t in zip(cal_fidxs, cal_ftimes)
    }
    fidx_min = int(cal_fidxs[0])
    fidx_max = int(cal_fidxs[-1])

    # --- ECG ---
    ecg_times: np.ndarray | None = None
    ecg_vals: np.ndarray | None = None
    if "ShimmerECG" in by_name:
        ecg = by_name["ShimmerECG"]
        ts = np.asarray(ecg_corrected_ts if ecg_corrected_ts is not None
                        else ecg["time_stamps"])
        lag = lag_ms.get("ShimmerECG")
        ts = ts - (lag / 1000.0 if lag else 0.0)
        raw = np.asarray(ecg["time_series"])
        lead1 = raw[:, 1] if raw.ndim == 2 and raw.shape[1] > 1 else raw[:, 0]
        ecg_times = ts
        ecg_vals = lead1.astype(np.float32)

    # --- Audio envelope (pre-computed once) ---
    audio_times: np.ndarray | None = None
    audio_env: np.ndarray | None = None
    if "Audio" in by_name:
        try:
            from scipy.signal import butter, filtfilt, hilbert
            audio = by_name["Audio"]
            a_ts = np.asarray(audio["time_stamps"])
            lag = lag_ms.get("Audio")
            a_ts = a_ts - (lag / 1000.0 if lag else 0.0)
            a_fs = float(audio["info"]["nominal_srate"][0])
            raw_a = np.asarray([v[0] for v in audio["time_series"]], dtype=float)
            lo, hi = 1000.0, min(6000.0, a_fs / 2.0 * 0.95)
            b, a_coeff = butter(4, [lo / (a_fs / 2), hi / (a_fs / 2)], btype="band")
            env = np.abs(hilbert(filtfilt(b, a_coeff, raw_a)))
            # Downsample to ~1 kHz so per-frame extraction stays fast
            ds = max(1, int(a_fs / 1000))
            audio_times = a_ts[::ds].astype(np.float64)
            audio_env = env[::ds].astype(np.float32)
        except Exception as exc:
            print(f"[sync_validation] audio pre-processing failed: {exc}", file=sys.stderr)

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
    total_h = frame_h + _STRIP_H * 2

    out_path = out_dir / "sync_validation.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(out_path), fourcc, fps_out, (out_w, total_h))

    # --- Render frames sequentially ---
    cap.set(cv2.CAP_PROP_POS_FRAMES, float(fidx_min))
    current_fidx = fidx_min
    frames_written = 0

    while current_fidx <= fidx_max:
        ret, frame = cap.read()
        if not ret:
            break

        if current_fidx in fidx_to_time:
            t_now = fidx_to_time[current_fidx]

            cam = cv2.resize(frame, (out_w, frame_h))

            if ecg_times is not None and ecg_vals is not None:
                ecg_strip = _render_strip(
                    ecg_times, ecg_vals, t_now, _WINDOW_S,
                    press_ts, out_w, _STRIP_H, (255, 140, 60), "ECG lead1")
            else:
                ecg_strip = np.full((_STRIP_H, out_w, 3), (20, 20, 20), dtype=np.uint8)
                cv2.putText(ecg_strip, "ECG not available",
                            (out_w // 2 - 80, _STRIP_H // 2),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (150, 150, 150), 1)

            if audio_times is not None and audio_env is not None:
                aud_strip = _render_strip(
                    audio_times, audio_env, t_now, _WINDOW_S,
                    press_ts, out_w, _STRIP_H, (60, 200, 80), "Audio envelope (1–6 kHz)")
            else:
                aud_strip = np.full((_STRIP_H, out_w, 3), (20, 20, 20), dtype=np.uint8)
                cv2.putText(aud_strip, "Audio not available",
                            (out_w // 2 - 80, _STRIP_H // 2),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (150, 150, 150), 1)

            composite = np.vstack([cam, ecg_strip, aud_strip])

            # Red border flash on keyboard press
            if any(0.0 <= (t_now - tp) <= _FLASH_DUR_S for tp in press_ts):
                cv2.rectangle(composite, (0, 0),
                              (composite.shape[1] - 1, composite.shape[0] - 1),
                              (0, 0, 255), 10)

            # Elapsed time overlay
            cv2.putText(composite, f"t={t_now - cal_start:.2f}s",
                        (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.75,
                        (255, 255, 255), 2, cv2.LINE_AA)

            writer.write(composite)
            frames_written += 1
            if frames_written % 150 == 0:
                print(f"[sync_validation] {frames_written} frames written...",
                      file=sys.stderr)

        current_fidx += 1

    writer.release()
    cap.release()
    print(f"[sync_validation] wrote {frames_written} frames → {out_path.name}",
          file=sys.stderr)
    return out_path if frames_written > 0 else None


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

    out = generate_validation_video(
        by_name, None,
        {"ShimmerECG": None, "Audio": None, "VideoFrames": None},
        args.out_dir,
    )
    if out:
        print(f"wrote {out}")
        return 0
    print("ERROR: could not generate video (missing video file, frames.csv, "
          "or KeyboardFiducial stream)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
