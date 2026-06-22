"""The wizard FSM: full happy path + guards, driven headless with the
simulated fleet and fakes (no Qt, no hardware, no LabRecorder)."""
from __future__ import annotations

import pytest

from sensorchrono.config import SessionConfig
from sensorchrono.contract import StreamName
from sensorchrono.devices.base import (
    DeviceAdapter,
    LivenessReport,
    ReadyResult,
    StreamDef,
    StreamLiveness,
)
from sensorchrono.devices.simulated import default_simulated_fleet
from sensorchrono.orchestration import preflight
from sensorchrono.orchestration.postprocess_runner import PostprocessResult
from sensorchrono.orchestration.session import (
    InvalidTransition,
    SessionController,
    SessionState,
)


def _session(tmp_path, **over):
    kw = dict(participant="p01", session="s1", task="rest", duration_s=30, out_dir=tmp_path / "o", dry_run=True)
    kw.update(over)
    return SessionConfig(**kw)


class _FakeRecorder:
    def __init__(self):
        self.started = self.stopped = False

    def start(self, session, *, run=1):
        self.started = True

    def stop(self):
        self.stopped = True


def test_full_happy_path(tmp_path):
    rec = _FakeRecorder()
    result = PostprocessResult(overall_status="ok", audit_verdict="PASS")
    c = SessionController(
        _session(tmp_path),
        adapters=default_simulated_fleet(),
        recorder=rec,
        postprocess_fn=lambda xdf, mp4: result,
    )
    states: list[SessionState] = []
    c.state_changed.connect(lambda old, new: states.append(new))
    counts: list[int] = []
    c.fiducial_counted.connect(counts.append)

    c.run_preflight()
    assert c.state == SessionState.PREFLIGHT
    c.start_staging()
    assert c.state == SessionState.LIVENESS and c.staging_green
    c.start_calibration()
    assert c.state == SessionState.CALIBRATE and rec.started
    for t in [float(i) for i in range(12)]:  # 12 clean taps, ≥ min_count
        c.note_fiducial(t)
    assert c.fiducial_count >= 10 and counts[-1] >= 10
    c.to_recording()
    assert c.state == SessionState.RECORD and c.calibrated
    c.stop_recording()
    assert c.state == SessionState.DONE and rec.stopped
    assert c.postprocess_result is result
    assert states == [
        SessionState.PREFLIGHT, SessionState.LIVENESS, SessionState.CALIBRATE,
        SessionState.RECORD, SessionState.POSTPROCESS, SessionState.DONE,
    ]


def test_end_capture_then_finish_split(tmp_path):
    # The GUI ends capture (fast, -> POSTPROCESS) and only then runs the pipeline,
    # so it can paint the "post-processing…" page before finish() blocks.
    result = PostprocessResult(overall_status="ok", audit_verdict="PASS")
    c = SessionController(
        _session(tmp_path), adapters=default_simulated_fleet(),
        recorder=_FakeRecorder(), postprocess_fn=lambda xdf, mp4: result,
    )
    c.run_preflight(); c.start_staging(); c.start_calibration()
    for t in range(12):
        c.note_fiducial(float(t))
    c.to_recording()
    c.end_capture()
    assert c.state == SessionState.POSTPROCESS and c.postprocess_result is None
    c.finish()
    assert c.state == SessionState.DONE and c.postprocess_result is result


def test_finish_requires_postprocess_state(tmp_path):
    import pytest

    from sensorchrono.orchestration.session import InvalidTransition

    c = SessionController(_session(tmp_path), adapters=default_simulated_fleet())
    with pytest.raises(InvalidTransition):
        c.finish()  # not in POSTPROCESS


def test_invalid_transition_out_of_order(tmp_path):
    c = SessionController(_session(tmp_path), adapters=default_simulated_fleet())
    with pytest.raises(InvalidTransition):
        c.start_calibration()  # not in LIVENESS


def test_preflight_blocker_goes_to_error(tmp_path):
    bad = preflight.PreflightReport(
        checks=[preflight.CheckResult("camera", preflight.FAIL, "dead camera", required=True)]
    )
    c = SessionController(_session(tmp_path), adapters=default_simulated_fleet(), preflight_fn=lambda s: bad)
    c.run_preflight()
    assert c.state == SessionState.ERROR and c.error


class _NotGreenAdapter(DeviceAdapter):
    name = "ng"

    def streams(self):
        return [StreamDef.from_contract(StreamName.AUDIO)]

    def launch(self, session):
        pass

    def is_ready(self, timeout_s):
        return ReadyResult(True, "ng: ready")  # comes up fine...

    def check_liveness(self, window_s):
        # ...but never produces healthy samples
        return LivenessReport("ng", (StreamLiveness(
            StreamName.AUDIO, True, 0.0, 48000.0, 0.0, False, 0, 1, "no samples"),))

    def stop(self):
        pass


def test_liveness_not_green_blocks_calibration(tmp_path):
    c = SessionController(_session(tmp_path), adapters=[_NotGreenAdapter()])
    c.run_preflight()
    c.start_staging()
    assert c.state == SessionState.LIVENESS and not c.staging_green
    with pytest.raises(InvalidTransition):
        c.start_calibration()


def test_uncalibrated_recording_requires_opt_in(tmp_path):
    c = SessionController(_session(tmp_path), adapters=default_simulated_fleet(), postprocess_fn=lambda x, m: None)
    c.run_preflight()
    c.start_staging()
    c.start_calibration()
    c.note_fiducial(0.0)  # only one tap -> not calibrated
    with pytest.raises(InvalidTransition):
        c.to_recording(allow_uncalibrated=False)
    c.to_recording(allow_uncalibrated=True)  # explicit opt-in proceeds
    assert c.state == SessionState.RECORD and not c.calibrated


# -- teardown completeness (review fixes: no exit path strands resources) --
class _StopTrackingAdapter(DeviceAdapter):
    name = "track"

    def __init__(self):
        self.stopped = False

    def streams(self):
        return [StreamDef.from_contract(StreamName.AUDIO)]

    def launch(self, session):
        pass

    def is_ready(self, timeout_s):
        return ReadyResult(True, "track: ready")

    def check_liveness(self, window_s):
        return LivenessReport("track", (StreamLiveness(StreamName.AUDIO, True, 48000, 48000, 0.0, True, 1, 1, ""),))

    def stop(self):
        self.stopped = True


class _StopRecorder:
    def __init__(self):
        self.stopped = False

    def start(self, session, *, run=1):
        pass

    def stop(self):
        self.stopped = True


class _StopMonitor:
    def __init__(self):
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def snapshot(self):
        return LivenessReport("m", ())


class _StopLauncher:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def test_fail_tears_down_fleet_recorder_monitor(tmp_path):
    adapter, rec, mon = _StopTrackingAdapter(), _StopRecorder(), _StopMonitor()
    c = SessionController(_session(tmp_path), adapters=[adapter], recorder=rec, monitor=mon)
    c.run_preflight()
    c.start_staging()  # launches supervisor + monitor
    assert c.state == SessionState.LIVENESS
    c.fail("boom")
    assert c.state == SessionState.ERROR
    assert adapter.stopped and rec.stopped and mon.stopped  # nothing stranded


def test_teardown_stops_labrecorder_launcher(tmp_path):
    # The bundled-LabRecorder launcher must be killed on any exit path, so a
    # frozen run never leaves an orphaned LabRecorder.exe behind.
    adapter, rec, launcher = _StopTrackingAdapter(), _StopRecorder(), _StopLauncher()
    c = SessionController(
        _session(tmp_path), adapters=[adapter], recorder=rec, labrecorder_launcher=launcher,
    )
    c.run_preflight()
    c.start_staging()
    c.abort()
    assert c.state == SessionState.ERROR
    assert rec.stopped and launcher.stopped


def test_abort_routes_through_fail_and_tears_down(tmp_path):
    adapter = _StopTrackingAdapter()
    c = SessionController(_session(tmp_path), adapters=[adapter])
    c.run_preflight()
    c.start_staging()
    c.abort()
    assert c.state == SessionState.ERROR and adapter.stopped


def test_shutdown_tears_down_without_changing_state(tmp_path):
    adapter, mon = _StopTrackingAdapter(), _StopMonitor()
    c = SessionController(_session(tmp_path), adapters=[adapter], monitor=mon)
    c.run_preflight()
    c.start_staging()
    assert c.state == SessionState.LIVENESS
    c.shutdown()  # GUI window-close path: tear down but DON'T go to ERROR
    assert adapter.stopped and mon.stopped
    assert c.state == SessionState.LIVENESS



def test_phase_boundaries_are_written_on_fsm_transitions(tmp_path, monkeypatch):
    import json
    import sensorchrono.orchestration.session as session_mod

    times = iter([100.0, 110.0, 140.0])
    monkeypatch.setattr(session_mod, "_lsl_now", lambda: next(times))
    c = SessionController(
        _session(tmp_path), adapters=default_simulated_fleet(),
        postprocess_fn=lambda _xdf, _mp4: None,
    )
    c.run_preflight()
    c.start_staging()
    c.start_calibration()
    for t in range(12):
        c.note_fiducial(float(t))
    c.to_recording()
    c.end_capture()

    boundaries = json.loads(
        (c.session.out_dir / "phase_boundaries.json").read_text())
    assert boundaries == {
        "calibration_start_lsl": 100.0,
        "experiment_start_lsl": 110.0,
        "experiment_end_lsl": 140.0,
    }
