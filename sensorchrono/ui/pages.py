"""One widget per wizard state. Pages are dumb: they render what they're told
and emit a Qt signal when the operator acts; :class:`MainWindow` wires those
signals to the :class:`SessionController` and pushes state back to the pages.
"""
from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from sensorchrono.config import DeviceBindings
from sensorchrono.orchestration import device_scan
from sensorchrono.ui.theme import GOLD, WHITE, BLACK
from sensorchrono.ui.video_preview import VideoPreview
from sensorchrono.ui.waveform import AudioLevelMeter, WaveformWidget

_ASSETS = Path(__file__).parent / "assets"

_MIT_LICENSE = """\
MIT License

Copyright (c) 2024  Kennesaw State University
                    Center for Cyber Physical Realms

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.\
"""

_OK = "✓"
_WARN = "!"
_FAIL = "✗"

_MIC_DEFAULT = "(system default)"


def _heading(step: str, subtitle: str) -> QtWidgets.QLabel:
    """Styled page heading: gold step title + muted white subtitle."""
    lbl = QtWidgets.QLabel(
        f"<span style='color:{GOLD};font-size:15px;font-weight:bold;'>{step}</span><br>"
        f"<span style='color:#AAAAAA;font-size:12px;'>{subtitle}</span>")
    lbl.setTextFormat(QtCore.Qt.TextFormat.RichText)
    lbl.setContentsMargins(4, 6, 4, 6)
    return lbl


class SplashPage(QtWidgets.QWidget):
    """Welcome / license screen shown on first launch before the wizard."""

    proceed = QtCore.Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        from sensorchrono import __version__

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(60, 40, 60, 30)
        lay.setSpacing(0)

        # ── Logo ──────────────────────────────────────────────────────────
        logo_lbl = QtWidgets.QLabel()
        logo_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        logo_path = _ASSETS / "ksu_logo.png"
        if logo_path.exists():
            pix = QtGui.QPixmap(str(logo_path)).scaledToHeight(
                130, QtCore.Qt.TransformationMode.SmoothTransformation)
            logo_lbl.setPixmap(pix)
        else:
            logo_lbl.setText(
                f"<span style='color:{GOLD};font-size:36px;font-weight:bold;'>"
                f"KSU</span>")
        lay.addWidget(logo_lbl)
        lay.addSpacing(20)

        # ── App title ─────────────────────────────────────────────────────
        title = QtWidgets.QLabel(
            f"<span style='color:{GOLD};font-size:30px;font-weight:bold;"
            f"letter-spacing:3px;'>SensorChrono</span>")
        title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        title.setTextFormat(QtCore.Qt.TextFormat.RichText)
        lay.addWidget(title)
        lay.addSpacing(6)

        ver = QtWidgets.QLabel(
            f"<span style='color:#888;font-size:12px;'>version {__version__}</span>")
        ver.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        ver.setTextFormat(QtCore.Qt.TextFormat.RichText)
        lay.addWidget(ver)
        lay.addSpacing(4)

        inst = QtWidgets.QLabel(
            f"<span style='color:{WHITE};font-size:12px;'>"
            f"Kennesaw State University &nbsp;·&nbsp; "
            f"Center for Cyber Physical Realms</span>")
        inst.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        inst.setTextFormat(QtCore.Qt.TextFormat.RichText)
        lay.addWidget(inst)
        lay.addSpacing(24)

        # ── Divider ───────────────────────────────────────────────────────
        div = QtWidgets.QFrame()
        div.setFrameShape(QtWidgets.QFrame.Shape.HLine)
        div.setStyleSheet(f"color:#333;")
        lay.addWidget(div)
        lay.addSpacing(16)

        # ── License ───────────────────────────────────────────────────────
        lic_lbl = QtWidgets.QLabel(
            f"<span style='color:{GOLD};font-size:11px;font-weight:bold;'>"
            f"License</span>")
        lic_lbl.setTextFormat(QtCore.Qt.TextFormat.RichText)
        lay.addWidget(lic_lbl)
        lay.addSpacing(6)

        lic_box = QtWidgets.QPlainTextEdit(_MIT_LICENSE)
        lic_box.setReadOnly(True)
        lic_box.setFixedHeight(160)
        lic_box.setStyleSheet(
            f"background:#0D0D0D; color:#AAAAAA; font-family:'Courier New',monospace;"
            f"font-size:11px; border:1px solid #333; border-radius:4px; padding:6px;")
        lay.addWidget(lic_box)
        lay.addSpacing(20)

        # ── Get Started button ────────────────────────────────────────────
        start = QtWidgets.QPushButton("Get Started →")
        start.setFixedHeight(40)
        start.setFixedWidth(200)
        start.clicked.connect(self.proceed.emit)
        lay.addWidget(start, alignment=QtCore.Qt.AlignmentFlag.AlignCenter)
        lay.addStretch(1)


class SetupPage(QtWidgets.QWidget):
    started = QtCore.Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        form = QtWidgets.QFormLayout()
        self.participant = QtWidgets.QLineEdit()
        self.session = QtWidgets.QLineEdit()
        self.task = QtWidgets.QLineEdit()
        self.duration = QtWidgets.QSpinBox()
        self.duration.setRange(5, 14400)
        self.duration.setSuffix(" s")
        self.dry_run = QtWidgets.QCheckBox("dry run (synthetic streams, no hardware)")
        self.dry_run.toggled.connect(self._on_dry_run_toggled)
        self.out_dir = QtWidgets.QLabel()
        self.out_dir.setStyleSheet("color:#888;")
        form.addRow("Participant", self.participant)
        form.addRow("Session", self.session)
        form.addRow("Task", self.task)
        form.addRow("Duration", self.duration)
        form.addRow("", self.dry_run)
        form.addRow("Output dir", self.out_dir)

        self.bindings_group = self._build_bindings_group()

        self.error = QtWidgets.QLabel()
        self.error.setStyleSheet("color:#C0392B;font-weight:600;")
        self.error.setWordWrap(True)
        start = QtWidgets.QPushButton("Start session →")
        start.clicked.connect(self.started.emit)

        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(_heading(
            "Step 1 · Set up recording",
            "Connect your devices, fill in the labels, pick the hardware bindings, then Start session →"))
        lay.addLayout(form)
        lay.addWidget(self.bindings_group)
        lay.addWidget(self.error)
        lay.addStretch(1)
        lay.addWidget(start, alignment=QtCore.Qt.AlignmentFlag.AlignRight)

        # Cheap, non-intrusive scans (COM ports + mics) at construction; cameras
        # are only probed when the operator clicks "Rescan devices".
        self._populate_devices(probe_cameras=False)

    # -- device bindings ----------------------------------------------------
    def _build_bindings_group(self) -> QtWidgets.QGroupBox:
        group = QtWidgets.QGroupBox("Hardware bindings (real capture)")
        bform = QtWidgets.QFormLayout()
        self.shimmer_port = QtWidgets.QComboBox()
        self.shimmer_port.setEditable(True)
        self.shimmer_port.setToolTip(
            "COM port the Shimmer is paired on (Bluetooth shows up as a "
            "'Standard Serial over Bluetooth link' port)."
        )
        self.camera_index = QtWidgets.QComboBox()
        self.camera_index.setEditable(True)
        self.camera_index.setToolTip("OpenCV camera index for the webcam (usually 0).")
        self.mic_device = QtWidgets.QComboBox()
        self.mic_device.setEditable(True)
        self.mic_device.setToolTip("Audio input device for the mic; leave on system default if unsure.")
        self.rescan_devices = QtWidgets.QPushButton("Rescan devices")
        self.rescan_devices.clicked.connect(lambda: self._populate_devices(probe_cameras=True))
        bform.addRow("Shimmer COM port", self.shimmer_port)
        bform.addRow("Camera index", self.camera_index)
        bform.addRow("Microphone", self.mic_device)
        bform.addRow("", self.rescan_devices)
        group.setLayout(bform)
        return group

    def _populate_devices(self, *, probe_cameras: bool) -> None:
        """(Re)fill the binding dropdowns from a hardware scan, preserving any
        value the operator already chose and falling back to sensible defaults."""
        prev_port = self.shimmer_port.currentText().strip()
        prev_cam = self.camera_index.currentText().strip()
        prev_mic = self.mic_device.currentText().strip()

        ports = device_scan.serial_ports()
        self.shimmer_port.clear()
        self.shimmer_port.addItems([p.device for p in ports])
        # Prefer the operator's prior pick, else the first (Bluetooth-first) port.
        self.shimmer_port.setCurrentText(prev_port or (ports[0].device if ports else ""))

        cams = device_scan.cameras() if probe_cameras else []
        self.camera_index.clear()
        self.camera_index.addItems([str(i) for i in cams] or ["0", "1", "2", "3"])
        self.camera_index.setCurrentText(prev_cam or (str(cams[0]) if cams else "0"))

        self.mic_device.clear()
        self.mic_device.addItem(_MIC_DEFAULT, None)
        for m in device_scan.microphones():
            self.mic_device.addItem(f"{m.index}: {m.name}", m.index)
        self.mic_device.setCurrentText(prev_mic or _MIC_DEFAULT)

    @staticmethod
    def _parse_mic(text: str):
        text = text.strip()
        if not text or text == _MIC_DEFAULT:
            return None
        head = text.split(":", 1)[0].strip()
        return int(head) if head.isdigit() else text

    def _bindings_from_fields(self) -> DeviceBindings:
        port = self.shimmer_port.currentText().strip() or None
        cam_text = self.camera_index.currentText().strip()
        camera = int(cam_text) if cam_text.isdigit() else None
        return DeviceBindings(
            shimmer_com_port=port,
            shimmer_ecg_port=port,  # the BT COM port doubles as the ECG bridge port
            camera_index=camera,
            mic_device=self._parse_mic(self.mic_device.currentText()),
        )

    def _on_dry_run_toggled(self, checked: bool) -> None:
        # Bindings are only required for real capture; grey them out in dry-run
        # so the synthetic path stays one-click.
        self.bindings_group.setEnabled(not checked)

    def load(self, session) -> None:
        self.participant.setText(session.participant)
        self.session.setText(session.session)
        self.task.setText(session.task)
        self.duration.setValue(int(session.duration_s))
        self.dry_run.setChecked(bool(session.dry_run))
        self.out_dir.setText(str(session.out_dir))
        b = session.bindings
        if b.shimmer_com_port:
            self.shimmer_port.setCurrentText(str(b.shimmer_com_port))
        if b.camera_index is not None:
            self.camera_index.setCurrentText(str(b.camera_index))
        if b.mic_device is not None:
            self.mic_device.setCurrentText(str(b.mic_device))
        self._on_dry_run_toggled(bool(session.dry_run))
        self.error.clear()

    def apply_to(self, session) -> None:
        session.participant = self.participant.text().strip()
        session.session = self.session.text().strip()
        session.task = self.task.text().strip()
        session.duration_s = int(self.duration.value())
        session.dry_run = self.dry_run.isChecked()
        session.bindings = self._bindings_from_fields()

    def show_error(self, message: str) -> None:
        self.error.setText(message)


class PreflightPage(QtWidgets.QWidget):
    proceed = QtCore.Signal()
    rescan = QtCore.Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.list = QtWidgets.QListWidget()
        self._proceed = QtWidgets.QPushButton("Proceed to staging →")
        self._proceed.setEnabled(False)
        self._proceed.clicked.connect(self.proceed.emit)
        rescan = QtWidgets.QPushButton("Rescan")
        rescan.setProperty("role", "secondary")
        rescan.clicked.connect(self.rescan.emit)

        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(rescan)
        buttons.addStretch(1)
        buttons.addWidget(self._proceed)

        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(_heading(
            "Step 2 · Preflight — are the devices responding?",
            "Each device is opened and checked. Fix any ✗ (a warning ! is OK), then Proceed to staging →"))
        lay.addWidget(self.list)
        lay.addLayout(buttons)

    def update_report(self, report) -> None:
        self.list.clear()
        for c in report.checks:
            icon = {"pass": _OK, "warn": _WARN, "fail": _FAIL}.get(c.status, "?")
            req = "" if c.required else "  (optional)"
            self.list.addItem(f"{icon}  {c.name}: {c.detail}{req}")
        self._proceed.setEnabled(report.ok)


class LivenessPage(QtWidgets.QWidget):
    go_record = QtCore.Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["stream", "rate (Hz)", "ch", "ok"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)

        self.preview = VideoPreview()
        self.waveform = WaveformWidget()
        self.meter = AudioLevelMeter()

        right = QtWidgets.QVBoxLayout()
        right.addWidget(self.preview)
        right.addWidget(self.waveform)
        right.addWidget(self.meter)

        split = QtWidgets.QHBoxLayout()
        split.addWidget(self.table, 1)
        split.addLayout(right, 1)

        self._go = QtWidgets.QPushButton("Go to Recording →")
        self._go.setEnabled(False)
        self._go.clicked.connect(self.go_record.emit)

        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(_heading(
            "Step 3 · Staging — every stream live and healthy?",
            "Watch the live ECG trace + camera preview. When all streams read OK, Go to Recording →"))
        lay.addLayout(split)
        lay.addWidget(self._go, alignment=QtCore.Qt.AlignmentFlag.AlignRight)

    def update_report(self, report) -> None:
        self.table.setRowCount(len(report.streams))
        for i, s in enumerate(report.streams):
            cells = [str(s.name), f"{s.measured_rate_hz:.0f}/{s.expected_rate_hz:.0f}",
                     f"{s.measured_channels}/{s.expected_channels}", _OK if s.ok else _FAIL]
            for j, text in enumerate(cells):
                self.table.setItem(i, j, QtWidgets.QTableWidgetItem(text))
        self._go.setEnabled(report.ok)


class CalibratePage(QtWidgets.QWidget):
    done_calibration = QtCore.Signal(bool)  # allow_uncalibrated

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.count_label = QtWidgets.QLabel("0 / 0 clean taps")
        self.count_label.setStyleSheet("font-size:28px;")
        self.count_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.bar = QtWidgets.QProgressBar()
        hint = QtWidgets.QLabel("Tap the spacebar firmly about every 2 seconds (~15 times).")
        hint.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

        self._done = QtWidgets.QPushButton("Calibrated — start recording →")
        self._done.setEnabled(False)
        self._done.clicked.connect(lambda: self.done_calibration.emit(False))
        fallback = QtWidgets.QPushButton("Skip / accept fallback (uncalibrated) →")
        fallback.setProperty("role", "secondary")
        fallback.clicked.connect(lambda: self.done_calibration.emit(True))

        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(fallback)
        buttons.addStretch(1)
        buttons.addWidget(self._done)

        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(_heading(
            "Step 4 · Calibration block",
            "Tap the spacebar firmly ~15 times (~2 s apart). These anchor the audio/video lag measurement."))
        lay.addWidget(hint)
        lay.addStretch(1)
        lay.addWidget(self.count_label)
        lay.addWidget(self.bar)
        lay.addStretch(1)
        lay.addLayout(buttons)

    def update_count(self, count: int, min_count: int, calibrated: bool) -> None:
        self.count_label.setText(f"{count} / {min_count} clean taps")
        self.bar.setRange(0, max(1, min_count))
        self.bar.setValue(min(count, min_count))
        self._done.setEnabled(calibrated)


class RecordPage(QtWidgets.QWidget):
    stop_record = QtCore.Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.countdown = QtWidgets.QLabel("recording…")
        self.countdown.setStyleSheet("font-size:32px;")
        self.countdown.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.status = QtWidgets.QLabel("")
        self.status.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        stop = QtWidgets.QPushButton("Stop recording")
        stop.setProperty("role", "danger")
        stop.clicked.connect(self.stop_record.emit)

        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(_heading(
            "Step 5 · Recording",
            "Capturing all streams. Leave the devices in place — stops automatically, or press Stop."))
        lay.addStretch(1)
        lay.addWidget(self.countdown)
        lay.addWidget(self.status)
        lay.addStretch(1)
        lay.addWidget(stop, alignment=QtCore.Qt.AlignmentFlag.AlignCenter)

    def set_remaining(self, seconds: float) -> None:
        self.countdown.setText(f"{int(seconds)} s remaining")


class DonePage(QtWidgets.QWidget):
    start_another = QtCore.Signal()
    open_output = QtCore.Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.summary = QtWidgets.QLabel("")
        self.summary.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.summary.setWordWrap(True)
        self.out_dir_label = QtWidgets.QLabel("")
        self.out_dir_label.setStyleSheet("color:#888;")
        self.out_dir_label.setWordWrap(True)
        self._open = QtWidgets.QPushButton("📂 Open output folder")
        self._open.clicked.connect(self.open_output.emit)
        another = QtWidgets.QPushButton("Start another →")
        another.clicked.connect(self.start_another.emit)

        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self._open)
        buttons.addStretch(1)
        buttons.addWidget(another)

        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(_heading(
            "Step 7 · Done — your aligned dataset is ready",
            "Drift-corrected, lag-calibrated files are in the output folder."))
        lay.addWidget(self.summary)
        lay.addWidget(self.out_dir_label)
        lay.addStretch(1)
        lay.addLayout(buttons)

    def show_summary(self, controller) -> None:
        s = controller.session
        cal = "calibrated" if controller.calibrated else "uncalibrated (profile-default lags)"
        pp = controller.postprocess_result
        if pp is not None:
            verdict = pp.summary()
            headline = "✓ <b>Corrected, time-aligned dataset written</b> (drift-corrected, lag-subtracted)."
        else:
            verdict = "skipped — no .xdf found (dry-run, or LabRecorder saved outside the output folder)"
            headline = "Recording captured; automatic alignment did not run."
        self.summary.setText(
            f"{headline}<br><br>"
            f"<b>{s.participant} / {s.session} / {s.task}</b><br>"
            f"duration {s.duration_s}s · fiducials {controller.fiducial_count} · {cal}<br>"
            f"post-processing: {verdict}"
        )
        self.out_dir_label.setText(f"Output folder: {s.out_dir}")
        self._open.setEnabled(bool(s.out_dir))


class ErrorPage(QtWidgets.QWidget):
    retry = QtCore.Signal()
    abort = QtCore.Signal()
    open_logs = QtCore.Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.message = QtWidgets.QLabel("")
        self.message.setStyleSheet("color:#d44;")
        self.message.setWordWrap(True)
        retry = QtWidgets.QPushButton("Retry")
        retry.clicked.connect(self.retry.emit)
        abort = QtWidgets.QPushButton("Abort")
        abort.setProperty("role", "danger")
        abort.clicked.connect(self.abort.emit)
        # The full diagnostic detail (per-bridge ACK/TIMEOUT logs, COM-port
        # enumeration) lives on disk — give the operator one click to it so a
        # field failure can be zipped and sent to support.
        logs = QtWidgets.QPushButton("Open log folder")
        logs.clicked.connect(self.open_logs.emit)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(abort)
        buttons.addWidget(logs)
        buttons.addStretch(1)
        buttons.addWidget(retry)

        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(_heading("Something went wrong",
                               "Check the error below and the log folder for details."))
        lay.addWidget(self.message)
        lay.addStretch(1)
        lay.addLayout(buttons)

    def show_error(self, message: str) -> None:
        self.message.setText(message)
