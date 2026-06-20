"""Base class for adapters that drive a real capture-bridge subprocess.

Each concrete adapter (shimmer/camera/mic/keyboard) sets its bridge module,
readiness regex, and the flags it builds from a session, then this base wraps a
:class:`~sensorchrono.orchestration.supervisor.BridgeProcess` for spawn /
readiness / teardown.

For real captures the authoritative per-stream liveness comes from the
``LslMonitor`` (real LSL traffic). A real adapter's :meth:`check_liveness` only
reports *process health* (is the subprocess alive?) — it can't measure rate
itself, and says so in the note.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from sensorchrono.devices.base import (
    DeviceAdapter,
    LivenessReport,
    ReadyResult,
    StreamDef,
    StreamLiveness,
)
from sensorchrono.orchestration.supervisor import BridgeProcess, BridgeSpec

#: bridges run this much longer than the recording window so they outlast it
#: (staging + ~30 s calibration + the recording itself, plus margin)
DURATION_BUFFER_S = 120


def session_tag(session) -> str:
    """Filename-safe tag for bridge side-files (mp4/csv). All three parts are
    validated filename-safe by SessionConfig, so this is safe to embed."""
    return f"{session.participant}_{session.session}_{session.task}"


class BridgeAdapter(DeviceAdapter):
    #: subclass sets the importable bridge module, e.g.
    #: "sensorchrono.bridges.video_lsl_bridge"
    BRIDGE_MODULE: str = ""
    READY_PATTERN: re.Pattern[str] = re.compile(r"is live")  # subclass overrides

    def __init__(
        self,
        *,
        python: str = sys.executable,
        bridge_module: str | None = None,
    ) -> None:
        self._python = python
        self._bridge_module = bridge_module or self.BRIDGE_MODULE
        self._proc: BridgeProcess | None = None

    # -- subclasses implement ----------------------------------------------
    def streams(self) -> list[StreamDef]:  # pragma: no cover - abstract-ish
        raise NotImplementedError

    def _bridge_args(self, session) -> list[str]:  # pragma: no cover - abstract-ish
        raise NotImplementedError

    def _ready_pattern(self) -> re.Pattern[str]:
        return self.READY_PATTERN

    # -- shared lifecycle ---------------------------------------------------
    def build_argv(self, session) -> list[str]:
        """Construct the subprocess argv (pure — unit-testable).

        Mirrors ``postprocess_runner.build_command``: in a frozen PyInstaller
        build ``sys.executable`` is the bundled exe, not a Python interpreter,
        so ``-m module`` can't work — instead we re-invoke the exe with a
        ``--run-bridge <module>`` flag that the frozen entry dispatches to
        ``<module>.main(argv)``.
        """
        args = self._bridge_args(session)
        if getattr(sys, "frozen", False):
            return [self._python, "--run-bridge", self._bridge_module, *args]
        return [self._python, "-m", self._bridge_module, *args]

    def _stop_file(self, session) -> "Path | None":
        """Subclasses override to provide a stop-file path for clean teardown."""
        return None

    def launch(self, session) -> None:
        # cwd=None: the bridge is resolved by module name (dev ``-m`` finds the
        # package from the repo-root cwd the app inherits; frozen ``--run-bridge``
        # doesn't use cwd) and takes an explicit ``--out-dir``, so cwd is moot.
        # log_dir lives next to the recording it explains (<out_dir>/logs) so the
        # full per-bridge ACK/TIMEOUT sequence survives the session for triage.
        log_dir = Path(session.out_dir) / "logs"
        spec = BridgeSpec(self.name, self.build_argv(session), self._ready_pattern(),
                          cwd=None, log_dir=log_dir, stop_file=self._stop_file(session))
        self._proc = BridgeProcess(spec)
        self._proc.start()

    def is_ready(self, timeout_s: float) -> ReadyResult:
        if self._proc is None:
            return ReadyResult(False, f"{self.name}: not launched")
        return self._proc.wait_ready(timeout_s)

    def check_liveness(self, window_s: float) -> LivenessReport:
        alive = self._proc is not None and self._proc.is_alive()
        note = "" if alive else "bridge process not running"
        rows = [
            StreamLiveness(
                name=s.name, present=alive, measured_rate_hz=0.0,
                expected_rate_hz=s.nominal_rate_hz, max_gap_s=0.0, ok=alive,
                measured_channels=0, expected_channels=s.channels,
                note=note or "process alive; rate measured by LSL monitor",
            )
            for s in self.streams()
        ]
        return LivenessReport(self.name, tuple(rows))

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.stop()
            self._proc = None

    def recent_output(self) -> list[str]:
        return self._proc.recent_output() if self._proc is not None else []

    def _duration(self, session) -> float:
        return float(session.duration_s + DURATION_BUFFER_S)


def default_real_fleet(*, shimmer_mode: str = "ecg") -> list[DeviceAdapter]:
    """The proven v1 core driving the real bridges. Lazy imports avoid a
    module-load cycle (the adapter modules import this one)."""
    from sensorchrono.devices.camera import CameraAdapter
    from sensorchrono.devices.keyboard import KeyboardAdapter
    from sensorchrono.devices.microphone import MicrophoneAdapter
    from sensorchrono.devices.shimmer_exg import ShimmerExgAdapter

    return [ShimmerExgAdapter(mode=shimmer_mode), CameraAdapter(), MicrophoneAdapter(), KeyboardAdapter()]
