"""Pure liveness-verdict logic (no LSL) + a venv-only integration smoke test."""
from __future__ import annotations

import time

import pytest

from sensorchrono.config import SessionConfig
from sensorchrono.contract import STREAM_SPECS, StreamName
from sensorchrono.orchestration.lsl_monitor import compute_stream_liveness

ECG = STREAM_SPECS[StreamName.SHIMMER_ECG]  # 4ch @ 256 Hz
KB = STREAM_SPECS[StreamName.KEYBOARD_FIDUCIAL]  # marker, rate 0
DIAG = STREAM_SPECS[StreamName.SHIMMER_DIAGNOSTICS_ECG]  # 5ch @ 1 Hz


def test_present_full_rate_ok():
    r = compute_stream_liveness(ECG, present=True, n_samples=256, window_s=1.0, max_gap_s=0.004, measured_channels=4)
    assert r.ok and r.measured_rate_hz == 256.0


def test_absent_not_ok():
    r = compute_stream_liveness(ECG, present=False, n_samples=0, window_s=1.0, max_gap_s=0.0, measured_channels=0)
    assert not r.ok and not r.present


def test_under_rate_not_ok():
    r = compute_stream_liveness(ECG, present=True, n_samples=10, window_s=1.0, max_gap_s=0.004, measured_channels=4)
    assert not r.ok and "rate" in r.note


def test_wrong_channels_not_ok():
    r = compute_stream_liveness(ECG, present=True, n_samples=256, window_s=1.0, max_gap_s=0.004, measured_channels=2)
    assert not r.ok and "channels" in r.note


def test_big_gap_not_ok():
    r = compute_stream_liveness(ECG, present=True, n_samples=256, window_s=1.0, max_gap_s=1.0, measured_channels=4)
    assert not r.ok and "gap" in r.note


def test_marker_present_ok_regardless_of_rate_gap():
    r = compute_stream_liveness(KB, present=True, n_samples=0, window_s=1.0, max_gap_s=10.0, measured_channels=1)
    assert r.ok  # markers are judged on presence alone


def test_low_rate_stream_ok_with_sparse_window():
    # Regression: a 1 Hz stream (ShimmerDiagnostics_ECG) shows 0 samples in most
    # 0.5 s polls and ~1 s gaps by definition. It MUST still rate as healthy,
    # else the staging gate never goes green and "Go to Recording" stays disabled.
    r = compute_stream_liveness(DIAG, present=True, n_samples=0, window_s=0.5, max_gap_s=1.0, measured_channels=5)
    assert r.ok, r.note
    r2 = compute_stream_liveness(DIAG, present=True, n_samples=1, window_s=0.5, max_gap_s=1.2, measured_channels=5)
    assert r2.ok, r2.note


def test_low_rate_stream_stalled_still_fails():
    # A genuinely stalled 1 Hz stream (gap far beyond a few nominal periods) is
    # still caught — the gate stays meaningful, it's just rate-aware now.
    r = compute_stream_liveness(DIAG, present=True, n_samples=0, window_s=0.5, max_gap_s=10.0, measured_channels=5)
    assert not r.ok and "gap" in r.note


def test_monitor_resolves_real_simulated_stream(tmp_path):
    pytest.importorskip("pylsl")
    from sensorchrono.devices.simulated import SimulatedShimmerEXG
    from sensorchrono.orchestration.lsl_monitor import LslMonitor

    a = SimulatedShimmerEXG()
    a.launch(SessionConfig(participant="p", session="s", task="t", duration_s=30, out_dir=tmp_path / "o", dry_run=True))
    mon = LslMonitor([StreamName.SHIMMER_ECG])
    mon.start()
    try:
        deadline = time.time() + 6.0
        present = False
        while time.time() < deadline and not present:
            snap = mon.snapshot()
            present = bool(snap.streams) and any(s.present for s in snap.streams)
            time.sleep(0.2)
        assert present, "monitor never saw the simulated ShimmerECG outlet"
    finally:
        mon.stop()
        a.stop()
def test_low_but_continuous_video_rate_warns_without_blocking():
    from sensorchrono.contract import STREAM_SPECS, StreamName

    result = compute_stream_liveness(
        STREAM_SPECS[StreamName.VIDEO_FRAMES],
        present=True, n_samples=3, window_s=0.5,
        max_gap_s=0.2, measured_channels=2,
    )
    assert result.ok
    assert "low camera rate" in result.note


def test_stalled_video_still_blocks():
    from sensorchrono.contract import STREAM_SPECS, StreamName

    result = compute_stream_liveness(
        STREAM_SPECS[StreamName.VIDEO_FRAMES],
        present=True, n_samples=0, window_s=0.5,
        max_gap_s=1.0, measured_channels=2,
    )
    assert not result.ok