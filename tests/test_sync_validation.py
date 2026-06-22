from __future__ import annotations

import numpy as np

from analysis.sync_validation import (
    _STRIP_H,
    _base_stream_name,
    _build_waveform_traces,
    _stack_waveform_strips,
)


def _stream(times, values, rate, labels):
    channels = [{"label": [label]} for label in labels]
    return {
        "time_stamps": np.asarray(times, dtype=float),
        "time_series": np.asarray(values, dtype=float),
        "info": {
            "nominal_srate": [str(rate)],
            "desc": [{"channels": [{"channel": channels}]}],
        },
    }


def test_build_waveform_traces_includes_every_used_modality():
    eeg_t = np.arange(1280) / 128.0 + 100.0
    audio_t = np.arange(8000) / 8000.0 + 100.0
    ecg_t = np.arange(512) / 256.0 + 100.0
    motion_t = np.arange(320) / 32.0 + 100.0

    by_name = {
        "ShimmerECG": _stream(
            ecg_t,
            np.column_stack([
                ecg_t, np.sin(2 * np.pi * 5 * ecg_t),
                np.zeros((len(ecg_t), 2)),
            ]),
            256, ["device_time", "Lead_I", "Lead_II", "Lead_III"],
        ),
        "Audio": _stream(
            audio_t, np.sin(2 * np.pi * 2000 * audio_t)[:, None],
            8000, ["audio"],
        ),
        "Audio_1": _stream(
            audio_t, np.sin(2 * np.pi * 1800 * audio_t)[:, None],
            8000, ["audio"],
        ),
        "EmotivEEG": _stream(
            eeg_t,
            np.column_stack([
                20 * np.sin(2 * np.pi * 10 * eeg_t),
                np.zeros((len(eeg_t), 4)),
            ]),
            128, ["AF3", "T7", "Pz", "T8", "AF4"],
        ),
        "EmotivMotion": _stream(
            motion_t,
            np.column_stack([
                np.sin(2 * np.pi * motion_t),
                np.zeros((len(motion_t), 11)),
            ]),
            0, ["GYROX"] + [f"motion_{i}" for i in range(11)],
        ),
        "VideoFrames": _stream(motion_t, np.zeros((len(motion_t), 2)),
                               30, ["frame", "position"]),
        "KeyboardFiducial": _stream([100.0, 101.0], [[1], [0]], 0,
                                    ["event"]),
    }

    corrected_ecg = ecg_t + 0.5
    traces = _build_waveform_traces(
        by_name, corrected_ecg,
        {"ShimmerECG": 10.0, "Audio": 20.0},
    )

    assert [trace["name"] for trace in traces] == [
        "Audio", "Audio_1", "EmotivEEG", "ShimmerECG",
    ]
    assert np.isclose(traces[0]["times"][0], 99.98)
    assert np.isclose(traces[-1]["times"][0], corrected_ecg[0] - 0.010)
    assert "AF3" in traces[2]["label"]
    assert all(trace["name"] != "EmotivMotion" for trace in traces)


def test_stack_waveform_strips_is_vertical():
    camera = np.zeros((80, 160, 3), dtype=np.uint8)
    trace = {
        "times": np.linspace(0.0, 1.0, 20),
        "values": np.sin(np.linspace(0.0, 4.0, 20)),
        "color": (0, 255, 0),
        "label": "test",
    }
    composite = _stack_waveform_strips(
        camera, [trace, trace, trace], 1.0, np.array([0.5]))
    assert composite.shape == (80 + 3 * _STRIP_H, 160, 3)


def test_waveform_base_matching_uses_stream_boundary():
    assert _base_stream_name("Audio") == "Audio"
    assert _base_stream_name("Audio_2") == "Audio"
    assert _base_stream_name("EmotivEEG") == "EmotivEEG"
    assert _base_stream_name("AudioFoo") is None


def test_generate_validation_videos_uses_exact_phase_boundaries(
        tmp_path, monkeypatch):
    import json
    import analysis.sync_validation as validation

    (tmp_path / "phase_boundaries.json").write_text(json.dumps({
        "calibration_start_lsl": 10.0,
        "experiment_start_lsl": 20.0,
        "experiment_end_lsl": 50.0,
    }))
    keyboard = {
        "time_stamps": np.array([12.0, 18.0, 30.0]),
        "time_series": [["press"], ["press"], ["press"]],
    }
    calls = []

    def fake_generate(_by_name, _ecg_ts, _lag, out_dir, **kwargs):
        calls.append(kwargs)
        return out_dir / kwargs["output_name"]

    monkeypatch.setattr(validation, "generate_validation_video", fake_generate)
    outputs = validation.generate_validation_videos(
        {"KeyboardFiducial": keyboard}, None, {}, tmp_path)

    assert [path.name for path in outputs] == [
        "sync_validation.mp4", "experiment_validation.mp4",
    ]
    assert calls[0]["time_range"] == (10.0, 20.0)
    assert np.array_equal(calls[0]["event_ts"], [12.0, 18.0])
    assert calls[1]["time_range"] == (20.0, 50.0)
def test_write_aligned_audio_segment(tmp_path):
    import wave
    from analysis.sync_validation import _write_aligned_audio_segment

    fs = 100
    times = np.arange(300) / fs + 10.0
    stream = _stream(times, np.full((300, 1), 0.25), fs, ["audio"])
    output = tmp_path / "segment.wav"
    assert _write_aligned_audio_segment(
        stream, None, start_lsl=10.5, duration_s=1.0,
        output_path=output,
    )
    with wave.open(str(output), "rb") as wav:
        assert wav.getframerate() == fs
        assert wav.getnframes() == 100
        samples = np.frombuffer(wav.readframes(100), dtype=np.int16)
    assert np.all(samples > 8000)


def test_mux_microphone_audio_adds_audio_stream(tmp_path):
    import cv2
    import subprocess
    from imageio_ffmpeg import get_ffmpeg_exe
    from analysis.sync_validation import _mux_microphone_audio

    video = tmp_path / "experiment_validation.mp4"
    writer = cv2.VideoWriter(
        str(video), cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (64, 48))
    for i in range(10):
        writer.write(np.full((48, 64, 3), i * 10, dtype=np.uint8))
    writer.release()

    fs = 8000
    times = np.arange(fs * 3) / fs + 20.0
    signal = (0.2 * np.sin(2 * np.pi * 440 * times))[:, None]
    audio = _stream(times, signal, fs, ["audio"])
    assert _mux_microphone_audio(video, audio, None, start_lsl=20.5)

    probe = subprocess.run(
        [get_ffmpeg_exe(), "-i", str(video), "-f", "null", "-"],
        capture_output=True, text=True)
    assert "Audio: aac" in probe.stderr
