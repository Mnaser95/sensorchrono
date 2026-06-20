"""LabRecorder backends: RCS protocol against a fake server + fallback factory."""
from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

from sensorchrono.config import SessionConfig
from sensorchrono.orchestration.labrecorder import (
    ManualRecorder,
    RcsRecorder,
    RecorderError,
    build_filename_command,
    make_recorder,
)


def _session():
    return SessionConfig(
        participant="p01", session="s1", task="rest", duration_s=30,
        out_dir=Path("/tmp/o"), dry_run=True, root_label="sensorchrono",
    )


def test_filename_command_template():
    assert build_filename_command(_session(), run=2) == (
        "filename {root:sensorchrono}{task:rest}{participant:p01}{session:s1}{run:2}"
    )


class _FakeRcsServer:
    """Minimal LabRecorder RCS stand-in: records each newline command, replies OK."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("localhost", 0))
        self.sock.listen(1)
        self.host, self.port = self.sock.getsockname()
        self.received: list[str] = []
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        conn.settimeout(3.0)
        buf = b""
        try:
            while True:
                data = conn.recv(1024)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self.received.append(line.decode().strip())
                    conn.sendall(b"OK\n")
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self.sock.close()


def test_rcs_start_stop_sends_full_protocol():
    srv = _FakeRcsServer()
    try:
        rec = RcsRecorder(srv.host, srv.port, timeout=2.0)
        rec.start(_session())
        rec.stop()
        time.sleep(0.2)
        cmds = srv.received
        for expected in ("update", "select all", "start", "stop"):
            assert expected in cmds, f"missing {expected}: {cmds}"
        assert any(c.startswith("filename {root:sensorchrono}") for c in cmds)
        # 'select all' guarantees no stream can be under-selected
        assert cmds.index("select all") < cmds.index("start")
    finally:
        srv.close()


def test_is_available():
    srv = _FakeRcsServer()
    try:
        assert RcsRecorder.is_available(srv.host, srv.port)
    finally:
        srv.close()
    assert not RcsRecorder.is_available("localhost", 1, timeout=0.5)


def test_make_recorder_picks_rcs_when_server_up():
    # Proves the launch -> make_recorder handoff: once the launcher has an RCS
    # serving, make_recorder auto-selects RcsRecorder over the manual fallback.
    srv = _FakeRcsServer()
    try:
        rec = make_recorder(
            prefer_rcs=True, rcs_host=srv.host, rcs_port=srv.port,
            manual_prompt=lambda m: None, manual_confirm=lambda m: True,
        )
        assert isinstance(rec, RcsRecorder)
    finally:
        srv.close()


def test_make_recorder_falls_back_to_manual_when_rcs_down():
    rec = make_recorder(
        prefer_rcs=True, rcs_host="localhost", rcs_port=1,
        manual_prompt=lambda m: None, manual_confirm=lambda m: True,
    )
    assert isinstance(rec, ManualRecorder)


def test_make_recorder_raises_when_nothing_available():
    with pytest.raises(RecorderError):
        make_recorder(prefer_rcs=True, rcs_host="localhost", rcs_port=1)
