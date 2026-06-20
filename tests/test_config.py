"""SessionConfig validation is the app's idiot-proofing — these tests pin the
guardrail behaviour and the config.yaml round-trip."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sensorchrono.config import (
    DEFAULT_PROFILE_ID,
    MAX_DURATION_S,
    ConfigError,
    DeviceBindings,
    SessionConfig,
    default_dry_run,
)


def _valid(tmp_path: Path, **over) -> SessionConfig:
    kwargs = dict(
        participant="p01",
        session="s1",
        task="rest",
        duration_s=60,
        out_dir=tmp_path / "out",
        profile_id=DEFAULT_PROFILE_ID,
        dry_run=True,
    )
    kwargs.update(over)
    return SessionConfig(**kwargs)


def test_valid_dry_run_config_passes(tmp_path):
    _valid(tmp_path).validate()  # should not raise


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="documents the non-Windows default; on Windows real capture is the default (False)",
)
def test_default_dry_run_is_true_off_windows():
    assert default_dry_run() is True


def test_default_dry_run_is_false_on_windows():
    # Mirror of the above: on Windows (where the hardware lives) real capture is
    # the default, so dry-run defaults False.
    if sys.platform.startswith("win"):
        assert default_dry_run() is False


@pytest.mark.parametrize("label", ["", "   ", "has space", "bad/slash"])
def test_unsafe_labels_rejected(tmp_path, label):
    with pytest.raises(ConfigError):
        _valid(tmp_path, participant=label).validate()


def test_duration_bounds_enforced(tmp_path):
    with pytest.raises(ConfigError):
        _valid(tmp_path, duration_s=1).validate()
    with pytest.raises(ConfigError):
        _valid(tmp_path, duration_s=MAX_DURATION_S + 1).validate()


def test_unknown_profile_rejected(tmp_path):
    with pytest.raises(ConfigError):
        _valid(tmp_path, profile_id="nope").validate()


def test_real_capture_requires_device_bindings(tmp_path):
    with pytest.raises(ConfigError) as exc:
        _valid(tmp_path, dry_run=False).validate()
    msg = str(exc.value)
    assert "Shimmer" in msg and "camera" in msg


def test_real_capture_passes_with_bindings(tmp_path):
    cfg = _valid(
        tmp_path,
        dry_run=False,
        bindings=DeviceBindings(shimmer_com_ports=["COM7"], camera_indices=[0]),
    )
    cfg.validate()  # should not raise


def test_all_errors_collected_not_just_first(tmp_path):
    cfg = _valid(tmp_path, participant="", duration_s=0, profile_id="nope")
    with pytest.raises(ConfigError) as exc:
        cfg.validate()
    text = str(exc.value)
    assert "participant" in text and "duration_s" in text and "profile_id" in text


def test_yaml_round_trip_preserves_fields(tmp_path):
    cfg = _valid(
        tmp_path,
        bindings=DeviceBindings(shimmer_com_ports=["COM7"], camera_indices=[2],
                                mic_devices=["BRIO"]),
    )
    path = cfg.save(tmp_path / "config.yaml")
    loaded = SessionConfig.load(path)
    assert loaded.participant == cfg.participant
    assert loaded.duration_s == cfg.duration_s
    assert loaded.profile_id == cfg.profile_id
    assert loaded.bindings.shimmer_com_ports == ["COM7"]
    assert loaded.bindings.camera_indices == [2]
    assert isinstance(loaded.out_dir, Path)
