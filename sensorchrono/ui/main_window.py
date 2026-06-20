"""The QMainWindow shell: a QStackedWidget of wizard pages wired to a
:class:`SessionController`. The FSM is the single source of truth — page
buttons call controller transitions, and the controller's Signals drive which
page is shown and what it displays.

Threading note: FSM transitions run on the GUI thread. In dry-run they're fast
(no hardware waits, post-processing skipped). For real captures the long steps
(staging readiness, post-processing) should move to a worker QThread — flagged
for Phase 5. Liveness + the live feed are pulled by QTimers on the GUI thread,
which keeps cross-thread widget access safe.
"""
from __future__ import annotations

import time
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

import functools

from sensorchrono.config import ConfigError, SessionConfig, default_dry_run, user_config_path
from sensorchrono.contract import StreamName
from sensorchrono.orchestration.lsl_monitor import LslMonitor
from sensorchrono.orchestration.session import SessionController, SessionState
from sensorchrono.ui.pages import (
    CalibratePage,
    DonePage,
    ErrorPage,
    LivenessPage,
    PreflightPage,
    RecordPage,
    SetupPage,
    SplashPage,
)
from sensorchrono.ui.theme import APP_STYLESHEET, GOLD
from sensorchrono.ui.video_preview import synthetic_frame


_ASSETS = Path(__file__).parent / "assets"


class _BrandHeader(QtWidgets.QWidget):
    """Permanent top banner: KSU logo · SensorChrono · institution · license."""

    _H = 70

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(self._H)
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor("#000000"))
        self.setPalette(pal)

        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(18, 8, 20, 8)
        lay.setSpacing(14)

        # Logo (falls back to text if PNG not yet placed in assets/)
        logo_lbl = QtWidgets.QLabel()
        logo_path = _ASSETS / "ksu_logo.png"
        if logo_path.exists():
            pix = QtGui.QPixmap(str(logo_path)).scaledToHeight(
                self._H - 16, QtCore.Qt.TransformationMode.SmoothTransformation)
            logo_lbl.setPixmap(pix)
        else:
            logo_lbl.setText(
                f"<span style='color:{GOLD};font-size:20px;font-weight:bold;'>"
                f"KSU</span>")
        lay.addWidget(logo_lbl)

        # Vertical divider
        div = QtWidgets.QFrame()
        div.setFrameShape(QtWidgets.QFrame.Shape.VLine)
        div.setStyleSheet("color:#444;")
        lay.addWidget(div)

        # App name + institution
        name = QtWidgets.QLabel(
            f"<span style='color:{GOLD};font-size:17px;font-weight:bold;"
            f"letter-spacing:1px;'>SensorChrono</span><br>"
            f"<span style='color:#FFFFFF;font-size:11px;'>"
            f"Kennesaw State University"
            f"&nbsp;&nbsp;·&nbsp;&nbsp;"
            f"Center for Cyber Physical Realms</span>")
        name.setTextFormat(QtCore.Qt.TextFormat.RichText)
        lay.addWidget(name)

        lay.addStretch(1)

        # MIT license tag
        lic = QtWidgets.QLabel(
            "<span style='color:#888;font-size:10px;'>MIT License</span>")
        lic.setTextFormat(QtCore.Qt.TextFormat.RichText)
        lay.addWidget(lic)

_PAGE_ORDER = [
    SessionState.SETUP, SessionState.PREFLIGHT, SessionState.LIVENESS,
    SessionState.CALIBRATE, SessionState.RECORD, SessionState.POSTPROCESS,
    SessionState.DONE, SessionState.ERROR,
]


class LiveView(QtCore.QObject):
    """Pull ECG/audio off LSL and push synthetic video into the staging widgets
    via a GUI-thread QTimer. Best-effort: degrades silently without pylsl."""

    def __init__(self, page: LivenessPage, *, dry_run: bool, fps: int = 30,
                 preview_path=None) -> None:
        super().__init__()
        self._page = page
        self._dry_run = dry_run
        self._preview_path = preview_path  # JPEG the camera bridge drops ~2x/s
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(int(1000 / fps))
        self._timer.timeout.connect(self._tick)
        self._t0 = time.monotonic()
        self._ecg = None
        self._audio = None
        self._video = None
        self._ecg_ch: int | None = None  # which ECG channel actually carries signal
        self._video_frames = 0

    def start(self) -> None:
        self._resolve_inlets()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self._ecg = self._audio = self._video = None

    def _resolve_inlets(self) -> None:
        try:
            import pylsl
        except Exception:
            return
        for name, attr in ((StreamName.SHIMMER_ECG, "_ecg"), (StreamName.AUDIO, "_audio"),
                           (StreamName.VIDEO_FRAMES, "_video")):
            found = pylsl.resolve_byprop("name", str(name), 1, 0.5)
            if found:
                setattr(self, attr, pylsl.StreamInlet(found[0], max_buflen=2))

    def _pick_ecg_channel(self, samples) -> int:
        """Choose the ECG channel that actually carries signal. A Shimmer ECG
        stream pairs the live leads with constant status channels (e.g. ch0);
        plotting ch0 looks flatlined even when the heart trace is fine. Lock onto
        the highest-variance channel, re-picking only if it goes flat."""
        import numpy as np

        stds = np.asarray(samples, dtype=float).std(axis=0)
        if self._ecg_ch is None or self._ecg_ch >= len(stds) or stds[self._ecg_ch] < 1e-6:
            self._ecg_ch = int(np.argmax(stds))
        return self._ecg_ch

    def _tick(self) -> None:
        if self._ecg is not None:
            samples, _ = self._ecg.pull_chunk(timeout=0.0, max_samples=512)
            if samples:
                ch = self._pick_ecg_channel(samples)
                self._page.waveform.append([row[ch] for row in samples])
        if self._audio is not None:
            samples, _ = self._audio.pull_chunk(timeout=0.0, max_samples=4096)
            if samples:
                import numpy as np

                rms = float(np.sqrt(np.mean(np.square(np.asarray(samples, dtype=float)))))
                self._page.meter.set_level(min(1.0, rms * 4))
        if self._dry_run:
            # Dry-run has no real camera: a moving synthetic pattern gives the
            # staging page something to show.
            self._page.preview.set_frame(synthetic_frame(time.monotonic() - self._t0))
        else:
            # Real capture: the recording bridge holds the camera exclusively, so
            # the GUI can't open it. The bridge instead drops a small JPEG ~2x/s;
            # show it for a genuine live view, falling back to a status line until
            # the first snapshot lands (or if it goes stale).
            if self._video is not None:
                frames, _ = self._video.pull_chunk(timeout=0.0, max_samples=512)
                if frames:
                    self._video_frames += len(frames)
            shown = False
            if self._preview_path is not None:
                try:
                    import os
                    import time as _time

                    p = str(self._preview_path)
                    if os.path.exists(p) and (_time.time() - os.path.getmtime(p)) < 3.0:
                        shown = self._page.preview.show_image_file(p)
                except Exception:
                    shown = False
            if not shown:
                self._page.preview.show_status(
                    "● Recording to file\n"
                    f"{self._video_frames} frames captured\n"
                    "(camera preview starting…)"
                )


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, session: SessionConfig, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("SensorChrono — KSU Center for Cyber Physical Realms")
        self._base_session = session
        self.controller: SessionController | None = None
        self._monitor: LslMonitor | None = None
        self._live: LiveView | None = None
        self._record_t0 = 0.0

        # pages
        self.splash = SplashPage()
        self.setup = SetupPage()
        self.preflight = PreflightPage()
        self.liveness = LivenessPage()
        self.calibrate = CalibratePage()
        self.record = RecordPage()
        self.postprocess = QtWidgets.QLabel(
            f"<span style='color:{GOLD};font-size:16px;font-weight:bold;'>"
            f"Step 6 · Aligning &amp; cleaning your dataset…</span><br>"
            f"<span style='color:#AAAAAA;'>Drift correction · lag subtraction · sync validation</span>",
            alignment=QtCore.Qt.AlignmentFlag.AlignCenter,
        )
        self.postprocess.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.done = DonePage()
        self.error = ErrorPage()
        self._pages = {
            SessionState.SETUP: self.setup, SessionState.PREFLIGHT: self.preflight,
            SessionState.LIVENESS: self.liveness, SessionState.CALIBRATE: self.calibrate,
            SessionState.RECORD: self.record, SessionState.POSTPROCESS: self.postprocess,
            SessionState.DONE: self.done, SessionState.ERROR: self.error,
        }
        self.stack = QtWidgets.QStackedWidget()
        self.stack.addWidget(self.splash)          # index 0 — shown first
        for st in _PAGE_ORDER:
            self.stack.addWidget(self._pages[st])

        # Wrap header + stack in one container so the black banner spans the window
        container = QtWidgets.QWidget()
        vbox = QtWidgets.QVBoxLayout(container)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)
        vbox.addWidget(_BrandHeader())
        vbox.addWidget(self.stack)
        self.setCentralWidget(container)
        self.statusBar().showMessage("ready")

        # splash → setup
        self.splash.proceed.connect(lambda: self.stack.setCurrentWidget(self.setup))

        # page → controller wiring
        self.setup.started.connect(self._start_session)
        self.preflight.proceed.connect(self._go_staging)
        self.preflight.rescan.connect(self._rescan)
        self.liveness.go_record.connect(self._begin_calibration)
        self.calibrate.done_calibration.connect(self._to_recording)
        self.record.stop_record.connect(self._stop_recording)
        self.done.start_another.connect(self._restart)
        self.done.open_output.connect(self._open_output_folder)
        self.error.retry.connect(self._retry)
        self.error.abort.connect(self._abort)
        self.error.open_logs.connect(self._open_logs)

        # timers
        self._liveness_timer = QtCore.QTimer(self)
        self._liveness_timer.setInterval(500)
        self._liveness_timer.timeout.connect(self._refresh)

        self.setup.load(self._base_session)
        self.stack.setCurrentWidget(self.splash)

    # -- controller lifecycle ----------------------------------------------
    def _build_controller(self, session: SessionConfig) -> None:
        if session.dry_run:
            from sensorchrono.devices.simulated import default_simulated_fleet

            fleet = default_simulated_fleet()
        else:
            from sensorchrono.devices.bridge_adapter import default_real_fleet

            fleet = default_real_fleet()
        expected = [s.name for a in fleet for s in a.streams()]
        self._monitor = LslMonitor(expected)
        recorder, launcher, rcs_port = self._make_recorder(session)
        # Tell preflight to check the same port the bundled LabRecorder actually
        # uses, so it shows green instead of a misleading "RCS not reachable" warning.
        from sensorchrono.orchestration import preflight as _pf
        preflight_fn = functools.partial(_pf.check_all, rcs_port=rcs_port)
        self.controller = SessionController(
            session, adapters=fleet, monitor=self._monitor,
            recorder=recorder, labrecorder_launcher=launcher,
            preflight_fn=preflight_fn,
        )
        c = self.controller
        c.state_changed.connect(self._on_state)
        c.progress.connect(self.statusBar().showMessage)
        c.errored.connect(self.error.show_error)
        c.liveness_updated.connect(self.liveness.update_report)
        c.fiducial_counted.connect(self._on_fiducial)

    def _make_recorder(self, session: SessionConfig):
        """Return ``(recorder, launcher)``. For a real run, try to launch a
        bundled LabRecorder so its RCS is reachable *before* make_recorder picks
        a backend (RCS auto-wins). If no bundle / RCS never comes up, make_recorder
        falls back to the manual recorder — the launcher is still returned so the
        FSM tears it down. Dry-run takes neither.

        We use a dedicated port (22346) for the bundled instance so it never
        collides with a standalone LabRecorder the operator may already have open
        on the standard port 22345 (e.g. for EEG recordings). Without this the
        app would hijack the operator's LabRecorder, which has a different
        StudyRoot, and the XDF would land in the wrong folder."""
        if session.dry_run:
            from sensorchrono.orchestration.labrecorder import DEFAULT_RCS_PORT
            return None, None, DEFAULT_RCS_PORT
        from sensorchrono.orchestration.labrecorder import DEFAULT_RCS_PORT, make_recorder
        from sensorchrono.orchestration.labrecorder_launcher import (
            LabRecorderLauncher,
            bundled_labrecorder_dir,
        )

        # Port 22346 is reserved for the bundled instance so it never conflicts
        # with the operator's own LabRecorder on the standard port 22345.
        _BUNDLED_PORT = 22346

        launcher = None
        lr_dir = bundled_labrecorder_dir()
        if lr_dir is not None:
            launcher = LabRecorderLauncher(lr_dir, port=_BUNDLED_PORT)
            try:
                launcher.launch(session.out_dir)
            except Exception:
                pass  # RCS just won't be up; make_recorder falls back to manual

        def prompt(msg):
            QtWidgets.QMessageBox.information(self, "LabRecorder", msg)

        def confirm(msg):
            return QtWidgets.QMessageBox.question(self, "LabRecorder", msg) == QtWidgets.QMessageBox.StandardButton.Yes

        rcs_port = _BUNDLED_PORT if lr_dir is not None else DEFAULT_RCS_PORT
        try:
            recorder = make_recorder(
                rcs_port=rcs_port,
                manual_prompt=prompt,
                manual_confirm=confirm,
            )
        except Exception:
            recorder = None
        return recorder, launcher, rcs_port

    # -- transitions (page actions) ----------------------------------------
    def _start_session(self) -> None:
        self.setup.apply_to(self._base_session)
        # Snapshot the resolved session (incl. device bindings) so the log opens
        # with "here's exactly what this run was configured to capture".
        from sensorchrono.diagnostics_log import log_environment_snapshot

        log_environment_snapshot(self._base_session)
        self._persist_session()  # remember device bindings for next launch
        self._build_controller(self._base_session)
        try:
            self.controller.run_preflight()
        except ConfigError as exc:
            self.setup.show_error(str(exc))

    def _persist_session(self) -> None:
        """Best-effort: save the chosen session (incl. device bindings) so an
        admin binds the hardware once and later launches reload it. A failure to
        write must never block starting a recording."""
        try:
            self._base_session.save(user_config_path())
        except Exception:
            pass

    def _go_staging(self) -> None:
        self.controller.start_staging()
        if self.controller.state == SessionState.LIVENESS:
            self._start_live()

    def _rescan(self) -> None:
        try:
            self.controller.run_preflight()
        except ConfigError as exc:
            self.setup.show_error(str(exc))

    def _begin_calibration(self) -> None:
        self.controller.start_calibration()

    def _to_recording(self, allow_uncalibrated: bool) -> None:
        self.controller.to_recording(allow_uncalibrated=allow_uncalibrated)

    def _stop_recording(self) -> None:
        # Two-step so the "post-processing…" page paints before the blocking
        # pipeline runs: end capture (fast) -> render -> finish (analysis).
        self.controller.end_capture()
        QtWidgets.QApplication.processEvents()
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            self.controller.finish(xdf_path=self._recorded_xdf(), mp4_path=self._recorded_mp4())
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

    def _open_output_folder(self) -> None:
        from PySide6 import QtGui

        out = Path(self._base_session.out_dir)
        try:
            out.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(out)))

    def _open_logs(self) -> None:
        """Open the diagnostic logs: the session's ``<out_dir>/logs`` (per-bridge
        files) when it exists, else the app-wide ``~/.sensorchrono/logs``."""
        from PySide6 import QtGui

        from sensorchrono.diagnostics_log import log_dir as app_log_dir

        session_logs = Path(self._base_session.out_dir) / "logs"
        target = session_logs if session_logs.is_dir() else app_log_dir()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(target)))

    def _recorded_xdf(self) -> Path | None:
        """The .xdf LabRecorder just wrote, found as the newest under the
        session's output dir (the app sets LabRecorder's StudyRoot there). None
        in dry-run or if the operator drove LabRecorder to a different folder."""
        if self._base_session.dry_run:
            return None
        try:
            out = Path(self._base_session.out_dir)
            xdfs = sorted(out.rglob("*.xdf"), key=lambda p: p.stat().st_mtime, reverse=True)
            return xdfs[0] if xdfs else None
        except Exception:
            return None

    def _recorded_mp4(self) -> Path | None:
        if self._base_session.dry_run:
            return None
        from sensorchrono.devices.camera import CameraAdapter

        p = CameraAdapter().mp4_path(self._base_session)
        return p if p.exists() else None

    def _restart(self) -> None:
        if self.controller is not None:
            self.controller.shutdown()  # tear down the finished run's resources
        self._stop_live()
        self.setup.load(self._base_session)
        self.stack.setCurrentWidget(self.setup)

    def _retry(self) -> None:
        if self.controller is not None:
            try:
                self.controller.run_preflight()
            except ConfigError as exc:
                self.stack.setCurrentWidget(self.setup)
                self.setup.show_error(str(exc))

    def _abort(self) -> None:
        if self.controller is not None:
            self.controller.abort()  # tears down fleet + recorder + monitor
        self._stop_live()
        self.stack.setCurrentWidget(self.setup)

    # -- controller signal handlers ----------------------------------------
    def _on_state(self, old: SessionState, new: SessionState) -> None:
        self.stack.setCurrentWidget(self._pages[new])
        if new == SessionState.PREFLIGHT and self.controller.preflight_report:
            self.preflight.update_report(self.controller.preflight_report)
        elif new == SessionState.LIVENESS:
            if self.controller.last_liveness:
                self.liveness.update_report(self.controller.last_liveness)
            self._liveness_timer.start()
        elif new == SessionState.CALIBRATE:
            self.calibrate.update_count(0, self.controller._fiducial.min_count, False)
            self.setFocus()
        elif new == SessionState.RECORD:
            self._record_t0 = time.monotonic()
        elif new in (SessionState.DONE, SessionState.ERROR):
            self._stop_live()
            if new == SessionState.DONE:
                self.done.show_summary(self.controller)

    def _on_fiducial(self, count: int) -> None:
        self.calibrate.update_count(count, self.controller._fiducial.min_count, self.controller._fiducial.calibrated)

    def _refresh(self) -> None:
        c = self.controller
        if c is None:
            return
        if c.state in (SessionState.LIVENESS, SessionState.CALIBRATE, SessionState.RECORD):
            c.refresh_liveness()
        if c.state == SessionState.RECORD:
            remaining = c.session.duration_s - (time.monotonic() - self._record_t0)
            self.record.set_remaining(max(0.0, remaining))
            if remaining <= 0:
                self._stop_recording()

    # -- live feed + key capture -------------------------------------------
    def _start_live(self) -> None:
        if self._live is not None:
            self._live.stop()  # recreate per session so the preview path is current
        preview_path = None
        if not self._base_session.dry_run:
            from sensorchrono.devices.camera import CameraAdapter

            preview_path = CameraAdapter().preview_path(self._base_session)
        self._live = LiveView(
            self.liveness, dry_run=self._base_session.dry_run, preview_path=preview_path
        )
        self._live.start()

    def _stop_live(self) -> None:
        self._liveness_timer.stop()
        if self._live is not None:
            self._live.stop()
        if self._monitor is not None:
            self._monitor.stop()

    def keyPressEvent(self, event) -> None:
        if (event.key() == QtCore.Qt.Key.Key_Space and self.controller is not None
                and self.controller.state == SessionState.CALIBRATE):
            self.controller.note_fiducial(time.monotonic())
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        if self.controller is not None:
            self.controller.shutdown()  # never orphan bridges/recorder on close
        self._stop_live()
        super().closeEvent(event)


def _load_or_default_session() -> SessionConfig:
    """Reload the last saved session (device bindings included) if one exists,
    else seed a fresh default. A malformed saved config degrades to the default
    rather than crashing the app on launch."""
    path = user_config_path()
    if path.exists():
        try:
            return SessionConfig.load(path)
        except Exception:
            pass
    return SessionConfig(
        participant="p01", session="s1", task="rest", duration_s=60,
        out_dir=Path.home() / "sensorchrono_out", dry_run=default_dry_run(),
    )


def run(argv: list[str] | None = None) -> int:
    import sys

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv if argv is None else argv)
    app.setStyleSheet(APP_STYLESHEET)
    session = _load_or_default_session()
    win = MainWindow(session)
    win.resize(980, 720)
    win.show()
    return app.exec()
