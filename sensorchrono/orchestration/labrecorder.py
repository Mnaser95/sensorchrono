"""Drive LabRecorder headlessly so the operator never sees it.

Three backends behind one :class:`Recorder` interface, tried in order of
preference (see :func:`make_recorder`):

* :class:`RcsRecorder` — LabRecorder's Remote Control Server (TCP 22345):
  ``update`` → ``select all`` → ``filename {...}`` → ``start`` / ``stop``.
  **Primary**, but research flagged that the RCS socket may be absent from some
  released ``LabRecorder.exe`` builds — hence the fallbacks.
* :class:`CliRecorder` — ``LabRecorderCLI`` subprocess.
* :class:`ManualRecorder` — last resort: prompt the operator (via UI callbacks)
  to press Start/Stop themselves and confirm.

``select all`` is the *structural* fix for the historical "forgot to tick a
stream" failure: RCS can only select all-or-none, so the operator can't
under-select.
"""
from __future__ import annotations

import socket
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

DEFAULT_RCS_HOST = "localhost"
DEFAULT_RCS_PORT = 22345


class RecorderError(RuntimeError):
    pass


def build_filename_command(session, *, run: int = 1) -> str:
    """LabRecorder RCS filename command (no trailing newline).

    Uses ``{template:}`` so LabRecorder places the XDF directly in StudyRoot
    (which is already set to ``session.out_dir``) with no subdirectories —
    e.g. ``sensorchrono_out/p01_s1_rest.xdf`` — matching the flat layout of
    the other per-modality files (audio.wav, video.mp4, frames.csv).

    Yields e.g. ``filename {template:p01_s1_rest}`` or
    ``filename {template:p01_s1_rest_R2}`` for run > 1."""
    name = f"{session.participant}_{session.session}_{session.task}"
    if run > 1:
        name += f"_R{run}"
    return f"filename {{template:{name}}}"


class Recorder(ABC):
    name = "recorder"

    @abstractmethod
    def start(self, session, *, run: int = 1) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...


class RcsRecorder(Recorder):
    name = "rcs"

    def __init__(self, host: str = DEFAULT_RCS_HOST, port: int = DEFAULT_RCS_PORT, *, timeout: float = 5.0) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self._sock: socket.socket | None = None

    @staticmethod
    def is_available(host: str = DEFAULT_RCS_HOST, port: int = DEFAULT_RCS_PORT, *, timeout: float = 1.0) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _connect(self) -> None:
        if self._sock is None:
            try:
                self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            except OSError as exc:
                raise RecorderError(f"cannot reach LabRecorder RCS at {self.host}:{self.port}: {exc}") from exc

    def _send(self, command: str) -> str:
        self._connect()
        assert self._sock is not None
        try:
            self._sock.sendall((command + "\n").encode("utf-8"))
            self._sock.settimeout(self.timeout)
            try:
                return self._sock.recv(4096).decode("utf-8", "replace").strip()
            except socket.timeout:
                return ""  # some commands don't reply; absence is not an error
        except OSError as exc:
            raise RecorderError(f"RCS command {command!r} failed: {exc}") from exc

    def start(self, session, *, run: int = 1) -> None:
        self._send("update")
        self._send("select all")
        self._send(build_filename_command(session, run=run))
        self._send("start")

    def stop(self) -> None:
        try:
            self._send("stop")
        finally:
            if self._sock is not None:
                self._sock.close()
                self._sock = None


class CliRecorder(Recorder):
    name = "cli"

    def __init__(self, cli_path: str | Path, *, config: str | Path | None = None) -> None:
        self.cli_path = Path(cli_path)
        self.config = Path(config) if config else None
        self._proc: subprocess.Popen | None = None

    def build_argv(self) -> list[str]:
        argv = [str(self.cli_path)]
        if self.config:
            argv.append(str(self.config))
        return argv

    def start(self, session, *, run: int = 1) -> None:
        if not self.cli_path.exists():
            raise RecorderError(f"LabRecorderCLI not found at {self.cli_path}")
        self._proc = subprocess.Popen(self.build_argv())

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None


class ManualRecorder(Recorder):
    name = "manual"

    def __init__(self, *, prompt: Callable[[str], None], confirm: Callable[[str], bool]) -> None:
        self._prompt = prompt
        self._confirm = confirm

    def start(self, session, *, run: int = 1) -> None:
        self._prompt(
            "Open LabRecorder, tick ALL streams, set the filename, then press Start."
        )
        if not self._confirm("Is LabRecorder recording?"):
            raise RecorderError("operator did not confirm LabRecorder is recording")

    def stop(self) -> None:
        self._prompt("Press Stop in LabRecorder to finalise the .xdf.")
        if not self._confirm("Did LabRecorder stop and save the .xdf?"):
            raise RecorderError("operator did not confirm LabRecorder stopped")


def make_recorder(
    *,
    prefer_rcs: bool = True,
    rcs_host: str = DEFAULT_RCS_HOST,
    rcs_port: int = DEFAULT_RCS_PORT,
    cli_path: str | Path | None = None,
    manual_prompt: Callable[[str], None] | None = None,
    manual_confirm: Callable[[str], bool] | None = None,
) -> Recorder:
    """Pick the best available backend: RCS if reachable, else CLI if a path is
    given, else manual if UI callbacks are provided."""
    if prefer_rcs and RcsRecorder.is_available(rcs_host, rcs_port):
        return RcsRecorder(rcs_host, rcs_port)
    if cli_path is not None and Path(cli_path).exists():
        return CliRecorder(cli_path)
    if manual_prompt is not None and manual_confirm is not None:
        return ManualRecorder(prompt=manual_prompt, confirm=manual_confirm)
    raise RecorderError(
        "no recording backend available (RCS unreachable, no LabRecorderCLI path, no manual callbacks)"
    )
