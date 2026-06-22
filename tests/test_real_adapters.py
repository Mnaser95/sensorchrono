"""Real bridge adapters: command construction, the readiness-regex contract,
the headless-Shimmer guard, and a stub-launch lifecycle (no hardware)."""
from __future__ import annotations

import sys

from sensorchrono.config import DeviceBindings, SessionConfig
from sensorchrono.contract import StreamName
from sensorchrono.devices.base import DeviceAdapter
from sensorchrono.devices.bridge_adapter import default_real_fleet
from sensorchrono.devices.camera import CameraAdapter
from sensorchrono.devices.keyboard import KeyboardAdapter
from sensorchrono.devices.microphone import MicrophoneAdapter
from sensorchrono.devices.shimmer_exg import ShimmerExgAdapter


def _session(tmp_path, **over):
    kw = dict(participant="p01", session="s1", task="rest", duration_s=30, out_dir=tmp_path / "o", dry_run=False)
    kw.update(over)
    return SessionConfig(**kw)


def test_ready_patterns_match_real_bridge_output():
    # The exact lines the bridges print — the stringly-typed contract that
    # silently breaks if either side drifts.
    assert ShimmerExgAdapter()._ready_pattern().search("[COM3] LSL outlet: ShimmerECG @ 256 Hz")
    assert ShimmerExgAdapter(mode="emg")._ready_pattern().search("[COM5] LSL outlet: ShimmerEMG @ 512 Hz")
    assert CameraAdapter().READY_PATTERN.search("[video] LSL outlet 'VideoFrames' is live.")
    assert MicrophoneAdapter().READY_PATTERN.search("[audio] LSL outlet 'Audio' is live.")
    assert KeyboardAdapter().READY_PATTERN.search("[keyboard_fiducial] LSL outlet 'KeyboardFiducial' is live.")


def test_shimmer_argv_is_always_headless(tmp_path):
    a = ShimmerExgAdapter(com_port="COM3")
    argv = a.build_argv(_session(tmp_path, bindings=DeviceBindings(shimmer_com_ports=["COM3"])))
    # both deadlock traps must be defused: --no-prompt AND a positional mode
    assert "--no-prompt" in argv
    assert "ecg" in argv
    assert "--record-seconds" in argv
    assert argv[argv.index("--ecg-port") + 1] == "COM3"


def test_shimmer_rejects_bad_mode():
    import pytest

    with pytest.raises(ValueError):
        ShimmerExgAdapter(mode="eeg")


def test_camera_argv_and_mp4_path(tmp_path):
    a = CameraAdapter(camera_device_idx=2)
    s = _session(tmp_path, bindings=DeviceBindings(camera_indices=[2]))
    argv = a.build_argv(s)
    assert "--out-dir" in argv and str(s.out_dir) in argv
    assert argv[argv.index("--device") + 1] == "2"
    tag = argv[argv.index("--tag") + 1]
    assert a.mp4_path(s) == s.out_dir / f"{tag}_video.mp4"
    assert tag == "p01_s1_rest"
    # live-preview snapshot wired through for the staging page
    assert argv[argv.index("--preview-path") + 1] == str(a.preview_path(s))
    assert a.preview_path(s) == s.out_dir / f"{tag}_preview.jpg"


def test_build_argv_dev_uses_dash_m(tmp_path):
    # Dev: run the bridge as an importable module, NOT a loose script path.
    argv = CameraAdapter().build_argv(_session(tmp_path, bindings=DeviceBindings(camera_indices=[0])))
    assert argv[:3] == [sys.executable, "-m", "sensorchrono.bridges.video_lsl_bridge"]
    assert "--out-dir" in argv


def test_build_argv_frozen_uses_run_bridge(tmp_path, monkeypatch):
    # Frozen: sys.executable is the bundled exe (no `-m`), so we re-invoke it
    # with `--run-bridge <module>` that the frozen entry dispatches on. Mirrors
    # postprocess_runner's `--run-postprocess` handling.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    argv = CameraAdapter(python="SensorChrono.exe").build_argv(
        _session(tmp_path, bindings=DeviceBindings(camera_indices=[0]))
    )
    assert argv[:3] == ["SensorChrono.exe", "--run-bridge", "sensorchrono.bridges.video_lsl_bridge"]
    assert "--out-dir" in argv


def test_streams_are_canonical():
    assert {s.name for s in ShimmerExgAdapter().streams()} == {
        StreamName.SHIMMER_ECG, StreamName.SHIMMER_DIAGNOSTICS_ECG,
    }
    assert ShimmerExgAdapter(mode="emg").streams()[0].name == StreamName.SHIMMER_EMG
    assert CameraAdapter().streams()[0].name == StreamName.VIDEO_FRAMES
    assert MicrophoneAdapter().streams()[0].name == StreamName.AUDIO
    assert KeyboardAdapter().streams()[0].name == StreamName.KEYBOARD_FIDUCIAL


def test_check_liveness_before_launch_is_not_ok():
    assert CameraAdapter().check_liveness(0.1).ok is False


def test_default_real_fleet():
    fleet = default_real_fleet()
    # fleet_idx=0 for all adapters in default fleet → names end with _0
    assert {a.name for a in fleet} == {"shimmer_exg_0", "camera_0", "mic_0", "keyboard"}
    assert all(isinstance(a, DeviceAdapter) for a in fleet)


def test_adapter_launch_ready_stop_with_stub(tmp_path, monkeypatch):
    # A stub MODULE that mimics the video bridge's readiness line, then idles.
    # Bridges are spawned as `python -m <module>` now, so the stub must be
    # importable by the *child* process: put tmp_path on PYTHONPATH (inherited
    # by the subprocess) rather than only the parent's in-memory sys.path.
    stub = tmp_path / "stub_bridge.py"
    stub.write_text(
        "import sys, time\n"
        "print(\"[video] LSL outlet 'VideoFrames' is live.\")\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    a = CameraAdapter(bridge_module="stub_bridge")
    a.launch(_session(tmp_path, bindings=DeviceBindings(camera_indices=[0])))
    try:
        r = a.is_ready(5.0)
        assert r.ok, r.detail
        assert a.check_liveness(0.1).ok  # process is alive
    finally:
        a.stop()
    assert a.is_ready(0.1).ok is False  # torn down


def test_session_real_mode_builds_real_fleet(tmp_path):
    from sensorchrono.orchestration.session import SessionController

    s = _session(tmp_path, bindings=DeviceBindings(shimmer_com_ports=["COM3"], camera_indices=[0]))
    c = SessionController(s)
    assert {a.name for a in c._fleet()} == {"shimmer_exg_0", "camera_0", "mic_0", "keyboard"}
def test_video_playback_rate_mismatch_detection():
    from sensorchrono.bridges.video_lsl_bridge import _playback_rate_mismatch

    assert _playback_rate_mismatch(30.0, 5.0)
    assert not _playback_rate_mismatch(30.0, 29.0)
    assert not _playback_rate_mismatch(30.0, 0.0)
