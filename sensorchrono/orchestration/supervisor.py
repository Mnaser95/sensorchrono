"""Process + fleet supervision.

Two pieces:

* :class:`BridgeProcess` — spawn one capture-bridge subprocess, watch its
  stdout for a per-bridge readiness line, and tear it down gracefully
  (``terminate`` → wait → ``kill``). Phase-2 real adapters build one of these
  with their specific command + readiness regex; the Shimmer adapter is the
  reason the readiness regex is per-bridge (its line has no "is live").

* :class:`Supervisor` — own a *fleet* of :class:`DeviceAdapter` and drive the
  whole-fleet lifecycle: launch all, wait for all to be ready under one shared
  deadline, tear all down. Adapter-agnostic, so it works identically for the
  simulated fleet (dry-run) and the real one.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sensorchrono.devices.base import DeviceAdapter, ReadyResult

#: how many trailing stdout lines to surface in a readiness-failure message.
#: Field failures (e.g. a Shimmer's per-command "Configure EXG chip 1: TIMEOUT"
#: sequence) need more than the last line to be diagnosable; the full sequence
#: still lives in the per-bridge log file.
_FAIL_TAIL_LINES = 10


@dataclass
class BridgeSpec:
    """How to launch + recognise one capture-bridge subprocess."""

    name: str  # device id, e.g. "camera"
    argv: list[str]  # full command, e.g. [python, "-m", "sensorchrono.bridges.video_lsl_bridge", "--duration", "90", ...]
    ready_pattern: re.Pattern[str]  # matches the bridge's stdout readiness line
    cwd: Path | None = None
    #: when set, every stdout line is teed (timestamped) to
    #: ``<log_dir>/bridge_<name>.log`` — a session-scoped record that outlives the
    #: process and the 200-line ring buffer, so a field failure stays debuggable.
    log_dir: Path | None = None
    #: when set, stop() writes this file so the bridge can detect it and exit
    #: cleanly through its finally block before we resort to terminate().
    stop_file: Path | None = None


class BridgeProcess:
    """A single bridge subprocess with readiness detection + safe teardown."""

    def __init__(self, spec: BridgeSpec, *, max_log_lines: int = 200) -> None:
        self.spec = spec
        self._proc: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._ready = threading.Event()
        self._log: deque[str] = deque(maxlen=max_log_lines)
        self._lock = threading.Lock()
        #: per-bridge log file + its open handle (None unless spec.log_dir set).
        self.log_path: Path | None = None
        self._logfile = None

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._proc is not None:
            return  # idempotent
        # Force the child's stdout UNBUFFERED. A Python child writing to a pipe
        # block-buffers stdout by default, so a bridge's one-line readiness print
        # ("... is live") can sit unflushed for tens of seconds — long past the
        # readiness deadline — while liblsl's native (unbuffered C++) logging
        # streams through immediately. The reader thread then never sees the
        # match and staging wrongly fails even though the LSL stream is live.
        # PYTHONUNBUFFERED is honoured by both the dev interpreter (``-m``) and
        # the frozen PyInstaller exe (``--run-bridge``).
        self._open_logfile()
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        self._proc = subprocess.Popen(
            self.spec.argv,
            cwd=str(self.spec.cwd) if self.spec.cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # line-buffered on our (read) side
            env=env,
        )
        self._reader = threading.Thread(target=self._read_stdout, name=f"bridge-{self.spec.name}", daemon=True)
        self._reader.start()

    def _open_logfile(self) -> None:
        """Open the per-bridge log file if a ``log_dir`` was given. Best-effort:
        a logging failure must never stop a real capture from starting."""
        if self.spec.log_dir is None or self._logfile is not None:
            return
        try:
            self.spec.log_dir.mkdir(parents=True, exist_ok=True)
            self.log_path = self.spec.log_dir / f"bridge_{self.spec.name}.log"
            # line-buffered so each line is flushed as the bridge emits it.
            self._logfile = open(self.log_path, "a", encoding="utf-8", buffering=1)
        except Exception:
            self.log_path = None
            self._logfile = None

    def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.rstrip("\n")
            with self._lock:
                self._log.append(line)
            if self._logfile is not None:
                try:
                    self._logfile.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} {line}\n")
                except Exception:
                    pass  # a dead log file must not kill the readiness scan
            if not self._ready.is_set() and self.spec.ready_pattern.search(line):
                self._ready.set()

    def _failure_evidence(self) -> str:
        """The last N stdout lines + the full-log path, for a failure message.
        The caller (supervisor) frames the elapsed/timeout wording; this is just
        the evidence, so the timing isn't the misleading per-call residual."""
        tail = " | ".join(self.recent_output()[-_FAIL_TAIL_LINES:]) or "(no output)"
        if self.log_path is not None:
            return f"last output: {tail} — full log: {self.log_path}"
        return f"last output: {tail}"

    def wait_ready(self, timeout_s: float) -> ReadyResult:
        """Block until the readiness line appears, the process dies, or timeout."""
        start = time.monotonic()
        deadline = start + max(0.0, timeout_s)
        while True:
            if self._ready.is_set():
                return ReadyResult(True, f"{self.spec.name}: ready", time.monotonic() - start)
            rc = self.returncode
            if rc is not None:  # exited before printing readiness -> failure
                return ReadyResult(False, f"{self.spec.name}: exited rc={rc} before ready ({self._failure_evidence()})", time.monotonic() - start)
            if time.monotonic() >= deadline:
                return ReadyResult(False, f"{self.spec.name}: outlet never went live ({self._failure_evidence()})", time.monotonic() - start)
            time.sleep(0.02)

    def stop(self, term_grace_s: float = 5.0) -> None:
        """Graceful teardown: stop-file signal → wait → terminate → kill."""
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            # Phase 1: write the stop file so the bridge can exit cleanly through
            # its finally block (e.g. VideoWriter.release() to finalize the MP4
            # moov atom). On Windows, proc.terminate() is TerminateProcess() and
            # never runs the bridge's Python finally blocks.
            if self.spec.stop_file is not None:
                try:
                    self.spec.stop_file.write_text("")
                except Exception:
                    pass
                try:
                    proc.wait(timeout=term_grace_s)
                except subprocess.TimeoutExpired:
                    pass  # fall through to terminate

            # Phase 2: hard terminate if still alive
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=term_grace_s)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=term_grace_s)
                    except subprocess.TimeoutExpired:  # pragma: no cover - extreme
                        pass
        if self._reader is not None:
            self._reader.join(timeout=2.0)
            self._reader = None
        if self._logfile is not None:
            try:
                self._logfile.close()
            except Exception:  # pragma: no cover - defensive
                pass
            self._logfile = None
        self._proc = None

    # -- introspection ------------------------------------------------------
    @property
    def returncode(self) -> int | None:
        return self._proc.poll() if self._proc is not None else None

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def recent_output(self) -> list[str]:
        with self._lock:
            return list(self._log)


@dataclass
class FleetReadiness:
    """Aggregate readiness of every device in the fleet."""

    results: dict[str, ReadyResult] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(r.ok for r in self.results.values())

    def problems(self) -> list[str]:
        return [r.detail for r in self.results.values() if not r.ok]


class Supervisor:
    """Owns a fleet of adapters and drives the whole-fleet lifecycle."""

    def __init__(self, adapters: list[DeviceAdapter]) -> None:
        if not adapters:
            raise ValueError("Supervisor needs at least one device adapter")
        self.adapters = adapters
        self._launched = False

    def launch_all(self, session) -> None:
        for a in self.adapters:
            a.launch(session)
        self._launched = True

    def wait_until_ready(self, timeout_s: float, *, poll_s: float = 0.1) -> FleetReadiness:
        """Wait for every adapter to report ready under ONE shared deadline,
        polling all still-pending adapters each tick.

        The earlier version consumed the deadline *sequentially* — it called
        ``is_ready(remaining)`` per adapter in order, so a slow first device
        (e.g. a Shimmer's cold Bluetooth connect) could burn the entire budget
        and leave the rest with ~0 s, which then reported ``not ready within
        0.0s`` even though their streams were about to come (or had already come)
        up. Readiness latches, so instead we poll every pending adapter
        non-blockingly until all are ready or the shared deadline passes — each
        device effectively gets the *full* window, and a healthy fast device is
        never starved by a slow sibling. Returns as soon as all are ready."""
        staging_start = time.monotonic()
        deadline = staging_start + max(0.0, timeout_s)
        results: dict[str, ReadyResult] = {}
        pending = list(self.adapters)
        while pending and time.monotonic() < deadline:
            still: list[DeviceAdapter] = []
            for a in pending:
                r = a.is_ready(0.0)  # non-blocking: readiness has latched if it occurred
                if r.ok:
                    results[a.name] = r
                else:
                    still.append(a)
            pending = still
            if pending:
                time.sleep(poll_s)
        # Anything still pending genuinely failed to come up within the window.
        # Report the ACTUAL elapsed staging time + the configured timeout, never
        # the residual deadline (which is ~0 here and produced the infamous,
        # misleading "not ready within 0.0s"). The adapter's own detail supplies
        # the evidence (its last output + log-file path); we frame the timing.
        for a in pending:
            r = a.is_ready(0.0)
            if r.ok:  # latched ready in the final microseconds — keep the success
                results[a.name] = r
                continue
            elapsed = time.monotonic() - staging_start
            evidence = r.detail.removeprefix(f"{a.name}: ")
            results[a.name] = ReadyResult(
                False,
                f"{a.name}: not ready after {elapsed:.1f}s (timeout {timeout_s:.0f}s) — {evidence}",
                elapsed,
            )
        return FleetReadiness(results)

    def stop_all(self) -> list[tuple[str, Exception]]:
        """Tear every adapter down (reverse launch order), best-effort.
        Returns any per-adapter errors instead of raising, so one failure
        can't strand the others still running."""
        errors: list[tuple[str, Exception]] = []
        for a in reversed(self.adapters):
            try:
                a.stop()
            except Exception as exc:  # keep tearing the rest down
                errors.append((a.name, exc))
        self._launched = False
        return errors
