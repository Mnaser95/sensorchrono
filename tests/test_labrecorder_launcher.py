"""LabRecorder launcher: bundle discovery, RCS poll, LabRecorder.cfg render, and a
launch/stop lifecycle that never depends on a real LabRecorder.exe."""
from __future__ import annotations

import socket

from sensorchrono.orchestration.labrecorder_launcher import (
    LabRecorderLauncher,
    bundled_labrecorder_dir,
    render_config,
    wait_for_rcs,
)

_ENV = "SENSORCHRONO_LABRECORDER_DIR"


def _listening_socket():
    """A bound+listening socket on an ephemeral port — enough for the OS to
    complete a TCP handshake, so RcsRecorder.is_available() sees it as 'up'."""
    s = socket.socket()
    s.bind(("localhost", 0))
    s.listen(1)
    return s, s.getsockname()[1]


# -- bundled_labrecorder_dir -------------------------------------------------
def test_bundle_dir_none_on_dev(monkeypatch):
    monkeypatch.delenv(_ENV, raising=False)
    assert bundled_labrecorder_dir() is None  # no _MEIPASS, no override


def test_bundle_dir_env_override_hit(tmp_path, monkeypatch):
    monkeypatch.setenv(_ENV, str(tmp_path))
    assert bundled_labrecorder_dir() == tmp_path


def test_bundle_dir_env_override_miss(tmp_path, monkeypatch):
    monkeypatch.setenv(_ENV, str(tmp_path / "does-not-exist"))
    assert bundled_labrecorder_dir() is None


def test_bundle_dir_frozen_requires_exe(tmp_path, monkeypatch):
    # _MEIPASS set but no LabRecorder/LabRecorder.exe -> not launchable...
    monkeypatch.delenv(_ENV, raising=False)
    monkeypatch.setattr("sys._MEIPASS", str(tmp_path), raising=False)
    assert bundled_labrecorder_dir() is None
    # ...until the exe is actually present.
    exe_dir = tmp_path / "LabRecorder"
    exe_dir.mkdir()
    (exe_dir / "LabRecorder.exe").write_text("stub")
    assert bundled_labrecorder_dir() == exe_dir


# -- render_config (keys verified against App-LabRecorder/LabRecorder.cfg) ----
def test_render_config_has_verified_keys(tmp_path):
    out = tmp_path / "out"
    cfg = render_config(out, port=22345)
    assert "RCSEnabled=1" in cfg
    assert "RCSPort=22345" in cfg
    assert f"StudyRoot={out.as_posix()}" in cfg


# -- wait_for_rcs ------------------------------------------------------------
def test_wait_for_rcs_true_when_serving():
    s, port = _listening_socket()
    try:
        assert wait_for_rcs("localhost", port, deadline_s=2.0) is True
    finally:
        s.close()


def test_wait_for_rcs_false_when_port_stays_closed():
    assert wait_for_rcs("localhost", 1, deadline_s=0.4, poll_interval_s=0.05) is False


# -- LabRecorderLauncher -----------------------------------------------------
def test_launch_reuses_existing_rcs_without_spawning(tmp_path):
    # An RCS already serving -> launch returns True and spawns nothing of its own,
    # so stop() must NOT try to kill a process it didn't start.
    s, port = _listening_socket()
    try:
        lr = LabRecorderLauncher(tmp_path, port=port)
        assert lr.launch(tmp_path / "out") is True
        assert lr._proc is None
    finally:
        s.close()
    lr.stop()  # idempotent no-op


def test_launch_returns_false_without_exe(tmp_path):
    # port 1 is closed -> no reuse; source dir has no LabRecorder.exe -> can't launch.
    lr = LabRecorderLauncher(tmp_path, port=1)
    assert lr.launch(tmp_path / "out") is False
    lr.stop()


def test_launch_writes_labrecorder_cfg_not_config_cfg(tmp_path):
    """The config MUST land in LabRecorder.cfg — that is the filename App-LabRecorder
    auto-loads from its own directory. Config.cfg is silently ignored.

    When source_dir is writable (the common case after Inno Setup installs to a
    per-user directory), the launcher runs LabRecorder in-place rather than copying
    it to a temp folder, which avoids Windows Defender flagging a new exe in %TEMP%.
    """
    import subprocess
    import unittest.mock as mock

    src = tmp_path / "LRsrc"
    src.mkdir()
    (src / "LabRecorder.exe").write_bytes(b"stub")

    launched_argv = []

    class _FakeProc:
        def poll(self): return 1  # immediately "exited"

    def fake_popen(argv, **kw):
        launched_argv.extend(argv)
        return _FakeProc()

    lr = LabRecorderLauncher(src, port=1)
    with mock.patch("subprocess.Popen", side_effect=fake_popen):
        lr.launch(tmp_path / "out")

    # In-place mode: config is written directly into source_dir (no temp copy).
    assert (src / "LabRecorder.cfg").exists(), "config must be LabRecorder.cfg"
    assert not (src / "Config.cfg").exists(), "Config.cfg must NOT be written"
    # Exe is launched from source_dir with no extra config path argument.
    assert len(launched_argv) == 1
    assert launched_argv[0] == str(src / "LabRecorder.exe")


def test_stop_is_idempotent(tmp_path):
    lr = LabRecorderLauncher(tmp_path, port=1)
    lr.stop()
    lr.stop()  # must not raise
