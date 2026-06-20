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


class _DeviceRow(QtWidgets.QWidget):
    """One row in a _MultiDeviceList: combo picker + remove button."""

    removed = QtCore.Signal(object)  # emits self

    def __init__(self, options: list[str], editable: bool = True, parent=None) -> None:
        super().__init__(parent)
        self.combo = QtWidgets.QComboBox()
        self.combo.setEditable(editable)
        self.combo.addItems(options)
        rm = QtWidgets.QPushButton("✕")
        rm.setFixedWidth(30)
        rm.setProperty("role", "danger")
        rm.clicked.connect(lambda: self.removed.emit(self))
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 2, 0, 2)
        row.setSpacing(4)
        row.addWidget(self.combo, 1)
        row.addWidget(rm)


class _MultiDeviceList(QtWidgets.QGroupBox):
    """Add / remove device list for one device type (Shimmer / Camera / Mic)."""

    changed = QtCore.Signal()

    def __init__(self, title: str, options_fn, editable: bool = True,
                 parent=None) -> None:
        super().__init__(title, parent)
        self._options_fn = options_fn  # callable → list[str] of current choices
        self._editable = editable
        self._rows: list[_DeviceRow] = []

        add_btn = QtWidgets.QPushButton("+ Add")
        add_btn.setProperty("role", "secondary")
        add_btn.setFixedWidth(70)
        add_btn.clicked.connect(self.add_row)

        hdr = QtWidgets.QHBoxLayout()
        hdr.addStretch(1)
        hdr.addWidget(add_btn)

        self._empty = QtWidgets.QLabel("(none — all disabled)")
        self._empty.setStyleSheet("color:#666;font-style:italic;padding:4px;")

        self._body = QtWidgets.QVBoxLayout()
        self._body.setSpacing(2)
        self._body.addWidget(self._empty)

        vbox = QtWidgets.QVBoxLayout(self)
        vbox.setSpacing(4)
        vbox.addLayout(hdr)
        vbox.addLayout(self._body)

    def add_row(self, value: str | None = None) -> _DeviceRow:
        opts = self._options_fn()
        row = _DeviceRow(opts, editable=self._editable)
        if value is not None:
            row.combo.setCurrentText(str(value))
        row.removed.connect(self._remove_row)
        self._rows.append(row)
        self._body.addWidget(row)
        self._empty.setVisible(False)
        self.changed.emit()
        return row

    def _remove_row(self, row: _DeviceRow) -> None:
        self._rows.remove(row)
        self._body.removeWidget(row)
        row.deleteLater()
        self._empty.setVisible(len(self._rows) == 0)
        self.changed.emit()

    def clear_rows(self) -> None:
        for row in list(self._rows):
            self._remove_row(row)

    def refresh_options(self) -> None:
        opts = self._options_fn()
        for row in self._rows:
            cur = row.combo.currentText()
            row.combo.clear()
            row.combo.addItems(opts)
            row.combo.setCurrentText(cur)

    def get_values(self) -> list[str]:
        return [r.combo.currentText().strip() for r in self._rows]


class SetupPage(QtWidgets.QWidget):
    started = QtCore.Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        # ── Session labels ────────────────────────────────────────────────
        form = QtWidgets.QFormLayout()
        self.participant = QtWidgets.QLineEdit()
        self.session_id = QtWidgets.QLineEdit()
        self.task = QtWidgets.QLineEdit()
        self.duration = QtWidgets.QSpinBox()
        self.duration.setRange(5, 14400)
        self.duration.setSuffix(" s")
        self.dry_run = QtWidgets.QCheckBox("dry run (synthetic streams, no hardware)")
        self.dry_run.toggled.connect(self._on_dry_run_toggled)
        self.out_dir = QtWidgets.QLabel()
        self.out_dir.setStyleSheet("color:#888;")
        form.addRow("Participant", self.participant)
        form.addRow("Session", self.session_id)
        form.addRow("Task", self.task)
        form.addRow("Duration", self.duration)
        form.addRow("", self.dry_run)
        form.addRow("Output dir", self.out_dir)

        # ── Available device lists (populated by scan) ────────────────────
        self._avail_ports: list[str] = ["COM3", "COM4", "COM5", "COM6"]
        self._avail_cams: list[str] = ["0", "1", "2", "3"]
        self._avail_mics: list[str] = [_MIC_DEFAULT]

        self._shimmer_list = _MultiDeviceList(
            "Shimmer devices  (ECG/EMG)",
            lambda: self._avail_ports, editable=True)
        self._camera_list = _MultiDeviceList(
            "Cameras  (UVC webcams)",
            lambda: self._avail_cams, editable=True)
        self._mic_list = _MultiDeviceList(
            "Microphones",
            lambda: self._avail_mics, editable=True)

        self._shimmer_list.changed.connect(self._refresh_display_options)
        self._camera_list.changed.connect(self._refresh_display_options)
        self._mic_list.changed.connect(self._refresh_display_options)

        self._hw_group = QtWidgets.QWidget()
        hw = QtWidgets.QVBoxLayout(self._hw_group)
        hw.setContentsMargins(0, 0, 0, 0)
        hw.setSpacing(6)
        hw.addWidget(self._shimmer_list)
        hw.addWidget(self._camera_list)
        hw.addWidget(self._mic_list)

        # ── Display layout ────────────────────────────────────────────────
        self._display_group = self._build_display_group()

        # ── Error + buttons ───────────────────────────────────────────────
        self.error = QtWidgets.QLabel()
        self.error.setStyleSheet("color:#C0392B;font-weight:600;")
        self.error.setWordWrap(True)

        rescan = QtWidgets.QPushButton("Rescan devices")
        rescan.setProperty("role", "secondary")
        rescan.clicked.connect(lambda: self._populate_devices(probe_cameras=True))
        start = QtWidgets.QPushButton("Start session →")
        start.clicked.connect(self.started.emit)
        btns = QtWidgets.QHBoxLayout()
        btns.addWidget(rescan)
        btns.addStretch(1)
        btns.addWidget(start)

        # ── Scrollable body ───────────────────────────────────────────────
        body = QtWidgets.QWidget()
        vbody = QtWidgets.QVBoxLayout(body)
        vbody.setContentsMargins(2, 2, 8, 2)
        vbody.setSpacing(10)
        vbody.addLayout(form)
        vbody.addWidget(self._hw_group)
        vbody.addWidget(self._display_group)
        vbody.addStretch(1)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(body)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)

        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(_heading(
            "Step 1 · Set up recording",
            "Add devices, fill in labels, design the display grid, then Start session →"))
        lay.addWidget(scroll, 1)
        lay.addWidget(self.error)
        lay.addLayout(btns)

        self._populate_devices(probe_cameras=False)

    # ── Display layout ────────────────────────────────────────────────────
    def _build_display_group(self) -> QtWidgets.QGroupBox:
        grp = QtWidgets.QGroupBox("Display layout (live monitor grid)")
        vbox = QtWidgets.QVBoxLayout(grp)

        size_row = QtWidgets.QHBoxLayout()
        size_row.addWidget(QtWidgets.QLabel("Grid:"))
        self._grid_rows = QtWidgets.QSpinBox()
        self._grid_rows.setRange(1, 4)
        self._grid_rows.setValue(2)
        self._grid_rows.setFixedWidth(50)
        self._grid_cols = QtWidgets.QSpinBox()
        self._grid_cols.setRange(1, 4)
        self._grid_cols.setValue(2)
        self._grid_cols.setFixedWidth(50)
        size_row.addWidget(self._grid_rows)
        size_row.addWidget(QtWidgets.QLabel("rows ×"))
        size_row.addWidget(self._grid_cols)
        size_row.addWidget(QtWidgets.QLabel("cols"))
        size_row.addStretch(1)
        vbox.addLayout(size_row)

        self._grid_rows.valueChanged.connect(self._rebuild_display_grid)
        self._grid_cols.valueChanged.connect(self._rebuild_display_grid)

        self._grid_widget = QtWidgets.QWidget()
        self._grid_layout = QtWidgets.QGridLayout(self._grid_widget)
        self._grid_layout.setSpacing(4)
        vbox.addWidget(self._grid_widget)

        self._cell_combos: list[list[QtWidgets.QComboBox]] = []
        self._rebuild_display_grid()
        return grp

    def _panel_options(self) -> list[tuple[str, str]]:
        opts: list[tuple[str, str]] = [("(empty)", "")]
        for i in range(len(self._camera_list.get_values())):
            opts.append((f"Video: Camera {i + 1}", f"video:{i}"))
        for i in range(len(self._shimmer_list.get_values())):
            opts.append((f"ECG: Shimmer {i + 1}", f"ecg:{i}"))
        for i in range(len(self._mic_list.get_values())):
            opts.append((f"Audio level: Mic {i + 1}", f"audio_level:{i}"))
            opts.append((f"Audio waveform: Mic {i + 1}", f"audio_wave:{i}"))
        return opts

    def _rebuild_display_grid(self) -> None:
        rows = self._grid_rows.value()
        cols = self._grid_cols.value()
        # Snapshot current values before clearing
        old: list[list[str]] = []
        for row_combos in self._cell_combos:
            old.append([cb.currentData() or "" for cb in row_combos])
        # Clear
        while self._grid_layout.count():
            item = self._grid_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        opts = self._panel_options()
        self._cell_combos = []
        for r in range(rows):
            row_combos: list[QtWidgets.QComboBox] = []
            for c in range(cols):
                cb = QtWidgets.QComboBox()
                for lbl, val in opts:
                    cb.addItem(lbl, val)
                # Restore previous value if available
                prev = old[r][c] if r < len(old) and c < len(old[r]) else ""
                for i in range(cb.count()):
                    if cb.itemData(i) == prev:
                        cb.setCurrentIndex(i)
                        break
                self._grid_layout.addWidget(cb, r, c)
                row_combos.append(cb)
            self._cell_combos.append(row_combos)

    def _refresh_display_options(self) -> None:
        opts = self._panel_options()
        for row_combos in self._cell_combos:
            for cb in row_combos:
                cur_val = cb.currentData()
                cb.clear()
                for lbl, val in opts:
                    cb.addItem(lbl, val)
                for i in range(cb.count()):
                    if cb.itemData(i) == cur_val:
                        cb.setCurrentIndex(i)
                        break

    # ── Device scan ───────────────────────────────────────────────────────
    def _populate_devices(self, *, probe_cameras: bool) -> None:
        ports = device_scan.serial_ports()
        self._avail_ports = [p.device for p in ports] or ["COM3", "COM4", "COM5", "COM6"]

        cams = device_scan.cameras() if probe_cameras else []
        self._avail_cams = [str(i) for i in cams] if cams else ["0", "1", "2", "3"]

        self._avail_mics = [_MIC_DEFAULT]
        for m in device_scan.microphones():
            self._avail_mics.append(f"{m.index}: {m.name}")

        self._shimmer_list.refresh_options()
        self._camera_list.refresh_options()
        self._mic_list.refresh_options()

    @staticmethod
    def _parse_mic(text: str):
        text = text.strip()
        if not text or text == _MIC_DEFAULT:
            return None
        head = text.split(":", 1)[0].strip()
        return int(head) if head.isdigit() else text

    def _bindings_from_fields(self) -> DeviceBindings:
        shimmer_ports = [p for p in self._shimmer_list.get_values() if p]
        camera_indices = [int(c) for c in self._camera_list.get_values()
                          if c.strip().isdigit()]
        mic_devices = [self._parse_mic(m) for m in self._mic_list.get_values()
                       if self._parse_mic(m) is not None]
        display_grid = [
            [cb.currentData() or "" for cb in row_combos]
            for row_combos in self._cell_combos
        ]
        return DeviceBindings(
            shimmer_com_ports=shimmer_ports,
            camera_indices=camera_indices,
            mic_devices=mic_devices,
            display_grid=display_grid,
        )

    def _on_dry_run_toggled(self, checked: bool) -> None:
        self._hw_group.setEnabled(not checked)

    # ── Public interface ──────────────────────────────────────────────────
    def load(self, session) -> None:
        self.participant.setText(session.participant)
        self.session_id.setText(session.session)
        self.task.setText(session.task)
        self.duration.setValue(int(session.duration_s))
        self.dry_run.setChecked(bool(session.dry_run))
        self.out_dir.setText(str(session.out_dir))

        b = session.bindings
        self._shimmer_list.clear_rows()
        for port in b.shimmer_com_ports:
            self._shimmer_list.add_row(port)

        self._camera_list.clear_rows()
        for cam in b.camera_indices:
            self._camera_list.add_row(str(cam))

        self._mic_list.clear_rows()
        for dev in b.mic_devices:
            self._mic_list.add_row(
                f"{dev}" if dev is not None else _MIC_DEFAULT)

        if b.display_grid:
            nrows = len(b.display_grid)
            ncols = max((len(r) for r in b.display_grid), default=1)
            self._grid_rows.setValue(nrows)
            self._grid_cols.setValue(ncols)
            self._rebuild_display_grid()
            for r, row_vals in enumerate(b.display_grid):
                for c, val in enumerate(row_vals):
                    if r < len(self._cell_combos) and c < len(self._cell_combos[r]):
                        cb = self._cell_combos[r][c]
                        for i in range(cb.count()):
                            if cb.itemData(i) == val:
                                cb.setCurrentIndex(i)
                                break

        self._on_dry_run_toggled(bool(session.dry_run))
        self.error.clear()

    def apply_to(self, session) -> None:
        session.participant = self.participant.text().strip()
        session.session = self.session_id.text().strip()
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
        self.table.setMaximumWidth(340)

        # Panel area — rebuilt by configure(); default = single device layout
        self._panel_container = QtWidgets.QWidget()
        self._panel_gl = QtWidgets.QGridLayout(self._panel_container)
        self._panel_gl.setSpacing(4)

        # Default single-device widgets (used when configure() is never called)
        self.preview = VideoPreview()
        self.waveform = WaveformWidget()
        self.meter = AudioLevelMeter()
        self._panel_gl.addWidget(self.preview, 0, 0)
        self._panel_gl.addWidget(self.waveform, 1, 0)
        self._panel_gl.addWidget(self.meter, 2, 0)

        # panels: stream_name → list of (widget, update_type) for LiveView
        self.panels: dict[str, list] = {}

        split = QtWidgets.QHBoxLayout()
        split.addWidget(self.table)
        split.addWidget(self._panel_container, 1)

        self._go = QtWidgets.QPushButton("Go to Recording →")
        self._go.setEnabled(False)
        self._go.clicked.connect(self.go_record.emit)

        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(_heading(
            "Step 3 · Staging — every stream live and healthy?",
            "Live feeds update automatically. When all streams read OK, Go to Recording →"))
        lay.addLayout(split)
        lay.addWidget(self._go, alignment=QtCore.Qt.AlignmentFlag.AlignRight)

    def configure(self, display_grid: list[list[str]]) -> None:
        """Rebuild the panel area from the 2-D display_grid spec set on SetupPage."""
        # Clear existing panels
        while self._panel_gl.count():
            item = self._panel_gl.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.panels.clear()

        if not display_grid:
            # Fall back to the default single-device layout
            self.preview = VideoPreview()
            self.waveform = WaveformWidget()
            self.meter = AudioLevelMeter()
            self._panel_gl.addWidget(self.preview, 0, 0)
            self._panel_gl.addWidget(self.waveform, 1, 0)
            self._panel_gl.addWidget(self.meter, 2, 0)
            return

        for r, row in enumerate(display_grid):
            for c, cell_type in enumerate(row):
                if not cell_type:
                    continue
                kind, _, idx_str = cell_type.partition(":")
                idx = int(idx_str) if idx_str.isdigit() else 0
                stream_name = {
                    "video": "VideoFrames" if idx == 0 else f"VideoFrames_{idx}",
                    "ecg": "ShimmerECG" if idx == 0 else f"ShimmerECG_{idx}",
                    "audio_level": "Audio" if idx == 0 else f"Audio_{idx}",
                    "audio_wave": "Audio" if idx == 0 else f"Audio_{idx}",
                }.get(kind)
                if stream_name is None:
                    continue
                if kind == "video":
                    w = VideoPreview()
                    if idx == 0:
                        self.preview = w
                elif kind == "ecg":
                    w = WaveformWidget()
                    if idx == 0:
                        self.waveform = w
                elif kind in ("audio_level", "audio_wave"):
                    w = AudioLevelMeter() if kind == "audio_level" else WaveformWidget()
                    if idx == 0 and kind == "audio_level":
                        self.meter = w
                else:
                    continue
                self.panels.setdefault(stream_name, []).append((w, kind))
                self._panel_gl.addWidget(w, r, c)

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
