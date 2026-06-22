"""The wizard finite-state machine — the operator journey.

``SETUP → PREFLIGHT → LIVENESS → CALIBRATE → RECORD → POSTPROCESS → DONE``
(+ ``ERROR`` from anywhere). :class:`SessionController` owns the supervisor,
liveness monitor, recorder, fiducial counter and post-process runner, exposes
guarded transition methods, and emits framework-agnostic
:class:`~sensorchrono.orchestration.events.Signal` events the GUI subscribes to.

Collaborators are injectable so the whole FSM is unit-testable headless with
the simulated fleet + fakes — no Qt, no hardware, no LabRecorder.
"""
from __future__ import annotations

import json
import time
from enum import StrEnum
from pathlib import Path
from typing import Callable

from sensorchrono.config import SessionConfig
from sensorchrono.devices.base import DeviceAdapter, LivenessReport, StreamLiveness
from sensorchrono.orchestration import preflight as preflight_mod
from sensorchrono.orchestration.events import Signal
from sensorchrono.orchestration.fiducial_live import FiducialCounter
from sensorchrono.orchestration.labrecorder import Recorder
from sensorchrono.orchestration.postprocess_runner import PostprocessResult, run_postprocess
from sensorchrono.orchestration.supervisor import FleetReadiness, Supervisor


class SessionState(StrEnum):
    SETUP = "setup"
    PREFLIGHT = "preflight"
    LIVENESS = "liveness"
    CALIBRATE = "calibrate"
    RECORD = "record"
    POSTPROCESS = "postprocess"
    DONE = "done"
    ERROR = "error"


class InvalidTransition(RuntimeError):
    pass


def _lsl_now() -> float:
    try:
        from pylsl import local_clock
        return float(local_clock())
    except Exception:
        return time.monotonic()


class SessionController:
    def __init__(
        self,
        session: SessionConfig,
        *,
        adapters: list[DeviceAdapter] | None = None,
        recorder: Recorder | None = None,
        labrecorder_launcher=None,
        monitor=None,
        preflight_fn: Callable[[SessionConfig], "preflight_mod.PreflightReport"] | None = None,
        fiducial: FiducialCounter | None = None,
        postprocess_fn: Callable[[Path | None, Path | None], PostprocessResult | None] | None = None,
        ready_timeout_s: float = 60.0,
    ) -> None:
        self.session = session
        self.state = SessionState.SETUP
        self.ready_timeout_s = ready_timeout_s

        self._adapters = adapters
        self._recorder = recorder
        # Any object with .stop(): the UI starts it before make_recorder() so the
        # RCS is reachable and gets auto-selected. Killed in _teardown_capture
        # AFTER recorder.stop() finalises the .xdf. None in dry-run / no bundle.
        self._labrecorder_launcher = labrecorder_launcher
        self._monitor = monitor
        self._preflight_fn = preflight_fn or preflight_mod.check_all
        self._fiducial = fiducial or FiducialCounter()
        self._postprocess_fn = postprocess_fn

        self.supervisor: Supervisor | None = None
        self.preflight_report: preflight_mod.PreflightReport | None = None
        self.last_liveness: LivenessReport | None = None
        self.readiness: FleetReadiness | None = None
        self.postprocess_result: PostprocessResult | None = None
        self.calibrated: bool = False
        self.error: str | None = None

        # signals (GUI connects Qt slots to these)
        self.state_changed = Signal("state_changed")  # (old, new)
        self.progress = Signal("progress")  # (message)
        self.errored = Signal("errored")  # (message)
        self.liveness_updated = Signal("liveness_updated")  # (LivenessReport)
        self.fiducial_counted = Signal("fiducial_counted")  # (count)

    # -- helpers ------------------------------------------------------------
    @property
    def fiducial_count(self) -> int:
        return self._fiducial.count

    def _goto(self, new: SessionState) -> None:
        old, self.state = self.state, new
        self.state_changed.emit(old, new)

    def _require(self, *states: SessionState) -> None:
        if self.state not in states:
            raise InvalidTransition(
                f"action not allowed in state {self.state}; expected one of {[s.value for s in states]}"
            )

    def _mark_phase(self, name: str, *, reset: bool = False) -> None:
        """Persist an LSL-clock phase boundary for post-processing."""
        try:
            path = Path(self.session.out_dir) / "phase_boundaries.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            if not reset and path.exists():
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            data[name] = _lsl_now()
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as exc:
            self.progress.emit(f"could not write phase boundary {name}: {exc}")

    def _teardown_capture(self) -> list[str]:
        """Stop the monitor, recorder, and device fleet — each guarded so one
        failure can't strand the others. Idempotent. Returns any errors."""
        errors: list[str] = []
        if self._monitor is not None:
            try:
                self._monitor.stop()
            except Exception as exc:
                errors.append(f"monitor.stop: {exc!r}")
        if self._recorder is not None:
            try:
                self._recorder.stop()
            except Exception as exc:
                errors.append(f"recorder.stop: {exc!r}")
        # Kill LabRecorder only AFTER recorder.stop() above has finalised the
        # .xdf (an RcsRecorder.stop sends "stop" over the socket first).
        if self._labrecorder_launcher is not None:
            try:
                self._labrecorder_launcher.stop()
            except Exception as exc:
                errors.append(f"labrecorder_launcher.stop: {exc!r}")
        if self.supervisor is not None:
            errors.extend(f"{name}.stop: {exc!r}" for name, exc in self.supervisor.stop_all())
        return errors

    def shutdown(self) -> list[str]:
        """Tear down all capture resources WITHOUT changing state — for the GUI
        window-close path (closing isn't an error). Idempotent."""
        return self._teardown_capture()

    def fail(self, message: str) -> None:
        """Force ERROR from anywhere, tearing down capture first so no failure
        path strands a running fleet/recorder/monitor. Any partial recording
        already on disk is preserved."""
        errors = self._teardown_capture()
        if errors:
            message = f"{message} | teardown issues: {'; '.join(errors)}"
        self.error = message
        self.errored.emit(message)
        self._goto(SessionState.ERROR)

    def _fleet(self) -> list[DeviceAdapter]:
        if self._adapters is not None:
            return self._adapters
        if self.session.dry_run:
            from sensorchrono.devices.simulated import default_simulated_fleet

            self._adapters = default_simulated_fleet()
        else:
            from sensorchrono.devices.bridge_adapter import default_real_fleet

            self._adapters = default_real_fleet()
        return self._adapters

    def _profile_lag_ms(self) -> dict[str, float]:
        try:
            from sensorchrono.profiles import load_profile

            prof = load_profile(self.session.profile_id)
            return {str(k): v for k, v in prof.fallback_lag_ms.items() if v is not None}
        except Exception:
            return {}

    # -- transitions --------------------------------------------------------
    def run_preflight(self) -> "preflight_mod.PreflightReport":
        self._require(SessionState.SETUP, SessionState.ERROR)
        self.session.validate()  # raises ConfigError on a bad config
        self.error = None
        report = self._preflight_fn(self.session)
        self.preflight_report = report
        self._goto(SessionState.PREFLIGHT)
        if not report.ok:
            self.fail("preflight blockers: " + "; ".join(c.detail for c in report.blockers()))
        return report

    def start_staging(self) -> FleetReadiness:
        self._require(SessionState.PREFLIGHT)
        self.supervisor = Supervisor(self._fleet())
        self.supervisor.launch_all(self.session)
        self.readiness = self.supervisor.wait_until_ready(self.ready_timeout_s)
        if not self.readiness.ok:
            self.fail("devices not ready: " + "; ".join(self.readiness.problems()))
            return self.readiness
        if self._monitor is not None:
            self._monitor.start()
        self._goto(SessionState.LIVENESS)
        self.refresh_liveness()
        return self.readiness

    def refresh_liveness(self, window_s: float = 1.0) -> LivenessReport:
        """Recompute the staging gate. Uses the LSL monitor's snapshot if one
        is attached, else aggregates each adapter's own liveness."""
        if self._monitor is not None:
            report = self._monitor.snapshot()
        else:
            rows: list[StreamLiveness] = []
            for a in (self.supervisor.adapters if self.supervisor else []):
                rows.extend(a.check_liveness(window_s).streams)
            report = LivenessReport(device="fleet", streams=tuple(rows))
        self.last_liveness = report
        self.liveness_updated.emit(report)
        return report

    @property
    def staging_green(self) -> bool:
        return self.last_liveness is not None and self.last_liveness.ok

    def start_calibration(self) -> None:
        self._require(SessionState.LIVENESS)
        self.refresh_liveness()
        if not self.staging_green:
            raise InvalidTransition(
                "cannot start recording: liveness not all green (" + "; ".join(self.last_liveness.problems()) + ")"
            )
        if self._recorder is not None:
            self._recorder.start(self.session)  # recording begins (incl. calibration block)
        self._mark_phase("calibration_start_lsl", reset=True)
        self._fiducial.reset()
        self._goto(SessionState.CALIBRATE)

    def note_fiducial(self, t: float) -> bool:
        """Feed a keystroke timestamp during calibration. Returns True if it
        was accepted as a clean fiducial (and emits the running count)."""
        self._require(SessionState.CALIBRATE)
        accepted = self._fiducial.offer(t)
        if accepted:
            self.fiducial_counted.emit(self._fiducial.count)
        return accepted

    def to_recording(self, *, allow_uncalibrated: bool = True) -> None:
        """Finish the calibration block and continue into the main recording.
        If too few clean fiducials were collected, only proceed when
        ``allow_uncalibrated`` (output will be labelled uncalibrated)."""
        self._require(SessionState.CALIBRATE)
        self.calibrated = self._fiducial.calibrated
        if not self.calibrated and not allow_uncalibrated:
            raise InvalidTransition(
                f"insufficient fiducials ({self._fiducial.count}/{self._fiducial.min_count}); "
                "retry or accept-fallback"
            )
        self._mark_phase("experiment_start_lsl")
        self._goto(SessionState.RECORD)

    def stop_recording(self, *, xdf_path: Path | None = None, mp4_path: Path | None = None) -> None:
        """End capture and post-process in one call (headless/test convenience).

        The GUI instead calls :meth:`end_capture` then :meth:`finish` so it can
        render the "post-processing…" page before the (blocking) pipeline runs."""
        self.end_capture()
        self.finish(xdf_path=xdf_path, mp4_path=mp4_path)

    def end_capture(self) -> None:
        """Stop the fleet + recorder and enter POSTPROCESS. Fast (no analysis),
        so the GUI can paint the progress page before :meth:`finish` blocks."""
        self._require(SessionState.RECORD)
        self._mark_phase("experiment_end_lsl")
        errors = self._teardown_capture()
        if errors:
            self.progress.emit("teardown issues: " + "; ".join(errors))
        self._goto(SessionState.POSTPROCESS)

    def finish(self, *, xdf_path: Path | None = None, mp4_path: Path | None = None) -> None:
        """Run the post-processing pipeline and enter DONE (or ERROR). The raw
        recording is already safely on disk, so a pipeline failure is reported,
        not silently swallowed."""
        self._require(SessionState.POSTPROCESS)
        try:
            self.postprocess_result = self._do_postprocess(xdf_path, mp4_path)
            self._goto(SessionState.DONE)
        except Exception as exc:
            self.fail(f"post-processing failed: {exc}")

    def _do_postprocess(self, xdf_path: Path | None, mp4_path: Path | None) -> PostprocessResult | None:
        if self._postprocess_fn is not None:
            return self._postprocess_fn(xdf_path, mp4_path)
        if xdf_path is None:
            # dry-run / no LabRecorder: nothing to post-process
            self.progress.emit("no XDF (dry-run); post-processing skipped")
            return None
        self.progress.emit("running post-processing pipeline…")
        return run_postprocess(
            xdf_path, self.session.out_dir, mp4=mp4_path, profile_lag_ms=self._profile_lag_ms(),
        )

    # -- recovery -----------------------------------------------------------
    def abort(self) -> None:
        """Hard stop from anywhere: tear everything down, end in ERROR.
        (fail() now performs the teardown, so this just routes through it.)"""
        self.fail("aborted by operator")
