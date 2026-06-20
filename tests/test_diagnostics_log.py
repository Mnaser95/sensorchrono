"""Central diagnostic logging: setup creates a rotating file, honours the env
overrides, and the environment snapshot lands in the log. Stdlib-only — runs on
a bare box."""
from __future__ import annotations

import logging
from pathlib import Path

from sensorchrono import diagnostics_log
from sensorchrono.config import DeviceBindings, SessionConfig


def _read_log(log_dir: Path) -> str:
    return (log_dir / "sensorchrono.log").read_text(encoding="utf-8")


def test_setup_creates_dir_and_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SENSORCHRONO_LOG_DIR", str(tmp_path / "logs"))
    out = diagnostics_log.setup_logging()
    assert out == tmp_path / "logs"
    logging.getLogger(diagnostics_log.LOGGER_NAME).info("hello field")
    assert "hello field" in _read_log(out)


def test_log_dir_honours_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("SENSORCHRONO_LOG_DIR", str(tmp_path / "elsewhere"))
    assert diagnostics_log.log_dir() == tmp_path / "elsewhere"


def test_log_dir_defaults_under_home(monkeypatch):
    monkeypatch.delenv("SENSORCHRONO_LOG_DIR", raising=False)
    assert diagnostics_log.log_dir() == Path.home() / ".sensorchrono" / "logs"


def test_debug_env_raises_file_level(tmp_path, monkeypatch):
    monkeypatch.setenv("SENSORCHRONO_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SENSORCHRONO_DEBUG", "1")
    out = diagnostics_log.setup_logging()
    logging.getLogger(diagnostics_log.LOGGER_NAME).debug("verbose detail")
    assert "verbose detail" in _read_log(out)


def test_debug_off_drops_debug_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("SENSORCHRONO_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("SENSORCHRONO_DEBUG", raising=False)
    out = diagnostics_log.setup_logging(debug=False)
    logging.getLogger(diagnostics_log.LOGGER_NAME).debug("should not appear")
    logging.getLogger(diagnostics_log.LOGGER_NAME).info("should appear")
    text = _read_log(out)
    assert "should appear" in text
    assert "should not appear" not in text


def test_setup_is_idempotent_no_duplicate_handlers(tmp_path, monkeypatch):
    monkeypatch.setenv("SENSORCHRONO_LOG_DIR", str(tmp_path / "logs"))
    diagnostics_log.setup_logging()
    diagnostics_log.setup_logging()
    diagnostics_log.setup_logging()
    owned = [h for h in logging.getLogger(diagnostics_log.LOGGER_NAME).handlers
             if getattr(h, diagnostics_log._OWNED, False)]
    # one file + one stderr handler (dev box is not frozen), never stacked.
    assert len(owned) == 2


def test_rotation_config_is_bounded(tmp_path, monkeypatch):
    import logging.handlers

    monkeypatch.setenv("SENSORCHRONO_LOG_DIR", str(tmp_path / "logs"))
    diagnostics_log.setup_logging()
    handlers = [h for h in logging.getLogger(diagnostics_log.LOGGER_NAME).handlers
                if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert handlers, "a rotating file handler must be installed"
    h = handlers[0]
    assert h.maxBytes > 0 and h.backupCount > 0  # bounded, won't fill the disk


def test_environment_snapshot_includes_bindings(tmp_path, monkeypatch):
    monkeypatch.setenv("SENSORCHRONO_LOG_DIR", str(tmp_path / "logs"))
    out = diagnostics_log.setup_logging()
    cfg = SessionConfig(
        participant="p", session="s", task="t", duration_s=30,
        out_dir=tmp_path / "o", dry_run=False,
        bindings=DeviceBindings(shimmer_com_ports=["COM3"], camera_indices=[0]),
    )
    diagnostics_log.log_environment_snapshot(cfg)
    text = _read_log(out)
    assert "environment snapshot" in text
    assert "COM3" in text  # the device binding that explains a COM3 failure
    assert "dry_run=False" in text


def test_environment_snapshot_without_config_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SENSORCHRONO_LOG_DIR", str(tmp_path / "logs"))
    diagnostics_log.setup_logging()
    diagnostics_log.log_environment_snapshot(None)  # must be a quiet no-op block
