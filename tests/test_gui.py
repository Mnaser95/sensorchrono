"""Headless GUI tests (offscreen Qt). Skipped where PySide6 isn't installed
(system Python); they run under the venv. They exercise FSM↔page wiring and the
custom widgets — not real LSL timing."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

import numpy as np
from PySide6 import QtWidgets

from sensorchrono.config import SessionConfig
from sensorchrono.contract import StreamName
from sensorchrono.devices.base import LivenessReport, StreamLiveness
from sensorchrono.orchestration.postprocess_runner import PostprocessResult
from sensorchrono.orchestration.session import SessionState
from sensorchrono.ui.main_window import MainWindow
from sensorchrono.ui.video_preview import VideoPreview, synthetic_frame
from sensorchrono.ui.waveform import AudioLevelMeter, WaveformWidget


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path, monkeypatch):
    # GUI flows persist the session to config.user_config_path() on start. Without
    # this redirect the suite would write to the developer's real
    # ~/.sensorchrono/config.yaml (it did, once — polluting a live install with a
    # dry-run config pointed at a pytest temp dir). Pin it to a throwaway path.
    monkeypatch.setenv("SENSORCHRONO_CONFIG", str(tmp_path / "user_config.yaml"))


def _session(tmp_path, **over):
    kw = dict(participant="p01", session="s1", task="rest", duration_s=15, out_dir=tmp_path / "o", dry_run=True)
    kw.update(over)
    return SessionConfig(**kw)


def test_window_starts_on_splash(app, tmp_path):
    w = MainWindow(_session(tmp_path))
    assert w.stack.currentWidget() is w.splash


def test_splash_proceeds_to_setup(app, tmp_path):
    w = MainWindow(_session(tmp_path))
    w.splash.proceed.emit()
    app.processEvents()
    assert w.stack.currentWidget() is w.setup


def test_setup_advances_to_preflight(app, tmp_path):
    w = MainWindow(_session(tmp_path))
    w.splash.proceed.emit()
    app.processEvents()
    w.setup.started.emit()
    app.processEvents()
    assert w.controller.state == SessionState.PREFLIGHT
    assert w.stack.currentWidget() is w.preflight
    # dry-run preflight is ok -> proceed button enabled
    assert w.preflight._proceed.isEnabled()


def test_setup_apply_collects_device_bindings(app, tmp_path):
    from sensorchrono.config import DeviceBindings

    w = MainWindow(_session(tmp_path, dry_run=False))
    w.setup._shimmer_list.add_row("COM7")
    w.setup._camera_list.add_row("2")
    # no mic added → empty list
    w.setup.apply_to(w._base_session)
    b = w._base_session.bindings
    assert b.shimmer_com_ports == ["COM7"]
    assert b.camera_indices == [2]
    assert b.mic_devices == []
    w._base_session.validate()  # real capture with bindings now validates


def test_setup_bindings_group_disabled_in_dry_run(app, tmp_path):
    w = MainWindow(_session(tmp_path, dry_run=True))
    w.setup.load(w._base_session)
    assert not w.setup._hw_group.isEnabled()
    w.setup.dry_run.setChecked(False)
    assert w.setup._hw_group.isEnabled()


def test_setup_load_restores_saved_bindings(app, tmp_path):
    from sensorchrono.config import DeviceBindings

    saved = _session(
        tmp_path, dry_run=False,
        bindings=DeviceBindings(shimmer_com_ports=["COM9"], camera_indices=[1],
                                mic_devices=[3]),
    )
    w = MainWindow(_session(tmp_path))
    w.setup.load(saved)
    assert w.setup._shimmer_list.get_values() == ["COM9"]
    assert w.setup._camera_list.get_values() == ["1"]
    assert w.setup._parse_mic(w.setup._mic_list.get_values()[0]) == 3


def test_session_persist_round_trip(app, tmp_path, monkeypatch):
    from sensorchrono.config import DeviceBindings, SessionConfig, user_config_path
    from sensorchrono.ui.main_window import _load_or_default_session

    monkeypatch.setenv("SENSORCHRONO_CONFIG", str(tmp_path / "cfg.yaml"))
    cfg = SessionConfig(
        participant="p01", session="s1", task="rest", duration_s=30,
        out_dir=tmp_path / "o", dry_run=False,
        bindings=DeviceBindings(shimmer_com_ports=["COM3"], camera_indices=[0]),
    )
    cfg.save(user_config_path())
    loaded = _load_or_default_session()
    assert loaded.bindings.shimmer_com_ports == ["COM3"]
    assert loaded.bindings.camera_indices == [0]
    assert loaded.dry_run is False


def test_invalid_config_keeps_setup_and_shows_error(app, tmp_path):
    w = MainWindow(_session(tmp_path))
    w.splash.proceed.emit()
    app.processEvents()
    w.setup.participant.setText("")  # empty -> ConfigError
    w.setup.started.emit()
    app.processEvents()
    assert w.stack.currentWidget() is w.setup
    assert w.setup.error.text()


def test_liveness_button_tracks_report(app, tmp_path):
    w = MainWindow(_session(tmp_path))
    green = LivenessReport("fleet", (StreamLiveness(StreamName.AUDIO, True, 48000, 48000, 0.0, True, 1, 1, ""),))
    w.liveness.update_report(green)
    assert w.liveness._go.isEnabled()
    red = LivenessReport("fleet", (StreamLiveness(StreamName.AUDIO, False, 0, 48000, 0.0, False, 0, 1, "absent"),))
    w.liveness.update_report(red)
    assert not w.liveness._go.isEnabled()


def test_calibrate_done_button_needs_threshold(app, tmp_path):
    w = MainWindow(_session(tmp_path))
    w.calibrate.update_count(3, 10, calibrated=False)
    assert not w.calibrate._done.isEnabled()
    w.calibrate.update_count(10, 10, calibrated=True)
    assert w.calibrate._done.isEnabled()


def test_video_preview_show_status_clears_frame(app):
    vp = VideoPreview()
    vp.resize(320, 180)
    vp.set_frame(synthetic_frame(1.0))
    assert vp.pixmap() is not None and not vp.pixmap().isNull()
    vp.show_status("● Recording to file\n12 frames captured")
    assert "Recording to file" in vp.text()
    assert vp.pixmap().isNull()  # the prior frame is cleared


def test_video_preview_show_image_file(app, tmp_path):
    # A real image on disk (as the camera bridge drops ~2x/s) must display; a bad
    # path must return False so the caller can fall back to the status line.
    from PySide6 import QtGui

    vp = VideoPreview()
    vp.resize(320, 180)
    arr = np.zeros((90, 160, 3), dtype=np.uint8)
    arr[:, :, 1] = 200  # green test frame
    qimg = QtGui.QImage(arr.data, 160, 90, 3 * 160, QtGui.QImage.Format.Format_RGB888).copy()
    img = tmp_path / "preview.png"
    assert qimg.save(str(img))
    assert vp.show_image_file(img) is True
    assert not vp.pixmap().isNull()
    assert vp.show_image_file(tmp_path / "nope.jpg") is False


def test_liveview_plots_varying_ecg_channel_not_constant_ch0(app):
    from sensorchrono.ui.main_window import LiveView
    from sensorchrono.ui.pages import LivenessPage

    lv = LiveView(LivenessPage(), dry_run=False)
    # ch0 + ch1 constant (status), ch2 varies most, ch3 varies less — like a real
    # Shimmer ECG frame. The preview must pick the live channel, not the flat ch0.
    samples = [[1.0, 5.0, float(i % 7), float(i % 3)] for i in range(64)]
    assert lv._pick_ecg_channel("ShimmerECG", samples) == 2
    # sticky: re-picks only if the chosen channel goes flat
    assert lv._pick_ecg_channel("ShimmerECG", samples) == 2


def test_widgets_render_without_error(app):
    wf = WaveformWidget(buffer_n=256)
    wf.append(np.sin(np.linspace(0, 10, 500)))  # more than the buffer
    assert wf._buf.shape == (256,)
    m = AudioLevelMeter()
    m.set_level(0.5)
    assert m.value() == 50
    vp = VideoPreview()
    vp.resize(320, 180)
    vp.set_frame(synthetic_frame(1.0))
    assert vp.pixmap() is not None and not vp.pixmap().isNull()


def test_done_page_summary(app, tmp_path):
    w = MainWindow(_session(tmp_path))
    w._build_controller(w._base_session)
    w.controller.calibrated = True
    w.controller.postprocess_result = PostprocessResult(overall_status="ok", audit_verdict="PASS")
    w.done.show_summary(w.controller)
    text = w.done.summary.text()
    assert "p01" in text and ("ok" in text or "PASS" in text)


def test_done_page_shows_output_folder_and_open_button(app, tmp_path):
    w = MainWindow(_session(tmp_path))
    w._build_controller(w._base_session)
    w.controller.calibrated = True
    w.controller.postprocess_result = PostprocessResult(overall_status="ok", audit_verdict="PASS")
    w.done.show_summary(w.controller)
    assert "aligned" in w.done.summary.text().lower()
    assert str(w._base_session.out_dir) in w.done.out_dir_label.text()
    assert w.done._open.isEnabled()
    fired = []
    w.done.open_output.connect(lambda: fired.append(True))
    w.done._open.click()
    assert fired  # the Open output folder button emits


def test_recorded_xdf_picks_newest_under_out_dir(app, tmp_path):
    import os
    import time

    from sensorchrono.config import DeviceBindings

    out = tmp_path / "o"
    out.mkdir()
    w = MainWindow(_session(
        tmp_path, dry_run=False, out_dir=out,
        bindings=DeviceBindings(shimmer_com_ports=["COM3"], camera_indices=[0]),
    ))
    (out / "old.xdf").write_bytes(b"x")
    time.sleep(0.05)
    newest = out / "new.xdf"
    newest.write_bytes(b"xx")
    os.utime(newest, None)  # bump mtime to now
    assert w._recorded_xdf() == newest


def test_recorded_xdf_none_in_dry_run(app, tmp_path):
    w = MainWindow(_session(tmp_path, dry_run=True))
    assert w._recorded_xdf() is None
    assert w._recorded_mp4() is None


def test_error_page_open_logs_button_emits(app, tmp_path):
    from sensorchrono.ui.pages import ErrorPage

    page = ErrorPage()
    page.show_error("devices not ready: shimmer_exg: not ready after 60.0s (timeout 60s)")
    fired = []
    page.open_logs.connect(lambda: fired.append(True))
    # find the button by its label and click it
    btn = next(b for b in page.findChildren(QtWidgets.QPushButton) if b.text() == "Open log folder")
    btn.click()
    assert fired


def test_open_logs_prefers_session_logs_dir(app, tmp_path, monkeypatch):
    monkeypatch.setenv("SENSORCHRONO_LOG_DIR", str(tmp_path / "applogs"))
    out = tmp_path / "o"
    (out / "logs").mkdir(parents=True)
    w = MainWindow(_session(tmp_path, out_dir=out))
    opened = {}
    from PySide6 import QtGui

    monkeypatch.setattr(QtGui.QDesktopServices, "openUrl", lambda url: opened.setdefault("url", url))
    w._open_logs()
    assert opened["url"].toLocalFile().rstrip("/").endswith("o/logs")


def test_close_event_tears_down_controller(app, tmp_path):
    w = MainWindow(_session(tmp_path))
    w._build_controller(w._base_session)
    called = []
    w.controller.shutdown = lambda: (called.append(True), [])[1]  # record the call
    w.close()
    assert called, "closeEvent must shut the controller down (no orphaned bridges)"
