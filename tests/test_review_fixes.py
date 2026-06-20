"""Regression tests for the Phase-0 adversarial-review findings. Each test
names the finding it pins so the intent survives. These cover the cases the
original suite missed (collision/precedence logic, malformed YAML, side-effect
freedom, the liveness gate's honesty, determinism)."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from sensorchrono.config import (
    DEFAULT_PROFILE_ID,
    ConfigError,
    DeviceBindings,
    SessionConfig,
)
from sensorchrono.contract import StreamName, spec
from sensorchrono.devices.simulated import (
    SimulatedKeyboard,
    SimulatedShimmerEXG,
    synth_audio_block,
    synth_ecg,
)
from sensorchrono.profiles import (
    ProfileError,
    _canonical_lag_key,
    _extract_lags,
    list_profiles,
    load_profile,
)


def _session(tmp_path: Path) -> SessionConfig:
    return SessionConfig(
        participant="p01", session="s1", task="rest",
        duration_s=30, out_dir=tmp_path / "out",
        profile_id=DEFAULT_PROFILE_ID, dry_run=True,
    )


# -- #4/#27 canonical lag-key mapping -------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Audio", StreamName.AUDIO),
        ("Audio_BRIO_via_USB", StreamName.AUDIO),
        ("VideoFrames_BRIO_via_USB", StreamName.VIDEO_FRAMES),
        ("ShimmerDiagnostics_ECG", StreamName.SHIMMER_DIAGNOSTICS_ECG),
        ("AudioFoo", None),  # no '_' boundary -> not a false Audio match
        ("Video", None),  # bare prefix, not a real name
        ("totally_unknown", None),
    ],
)
def test_canonical_lag_key(raw, expected):
    assert _canonical_lag_key(raw) == expected


# -- #8/#18 lag precedence -------------------------------------------------
def test_direct_null_lag_does_not_clobber_measured_descriptive():
    calib = {"lag_ms": {"Audio": None}, "lag_ms_other_modalities": {"Audio_BRIO_via_USB": 46.5}}
    assert _extract_lags(calib, Path("x"))[StreamName.AUDIO] == 46.5


def test_direct_measured_lag_wins_over_descriptive():
    calib = {"lag_ms": {"Audio": 99.0}, "lag_ms_other_modalities": {"Audio_BRIO_via_USB": 46.5}}
    assert _extract_lags(calib, Path("x"))[StreamName.AUDIO] == 99.0


# -- #7 malformed numeric YAML -> catchable ProfileError -------------------
def test_non_numeric_lag_raises_profile_error():
    with pytest.raises(ProfileError):
        _extract_lags({"lag_ms": {"Audio": "oops"}}, Path("x"))


def test_non_numeric_drift_raises_profile_error(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("profile_id: bad\ncalibration:\n  drift_ppm_observed:\n    median_ppm: notanumber\n")
    with pytest.raises(ProfileError):
        load_profile(p)


# -- #5 null bridge body tolerated ----------------------------------------
def test_null_bridge_body_is_tolerated(tmp_path):
    p = tmp_path / "half.yaml"
    p.write_text("profile_id: half\nbridges:\n  exg:\n")  # exg has a null body
    prof = load_profile(p)
    assert prof.bridge_defaults == {"exg": {}}
    assert prof.streams_emitted == {"exg": []}


# -- #1/#23 contract <-> profile cross-tier consistency --------------------
def test_contract_matches_every_profile_streams_emitted():
    for pid in list_profiles():
        prof = load_profile(pid)
        for bridge, streams in prof.streams_emitted.items():
            for s in streams:
                try:
                    sn = StreamName(s["name"])
                except ValueError:
                    continue
                exp = spec(sn)
                assert s["channels"] == exp.channels, f"{pid}/{bridge}/{s['name']} channels"
                if exp.nominal_rate_hz > 0 and s.get("rate_hz"):
                    assert float(s["rate_hz"]) == exp.nominal_rate_hz, f"{pid}/{bridge}/{s['name']} rate"


# -- #10/#17 dry_run reproducibility --------------------------------------
def test_dry_run_false_survives_round_trip(tmp_path):
    cfg = SessionConfig(
        participant="p", session="s", task="t", duration_s=60,
        out_dir=tmp_path / "o", profile_id=DEFAULT_PROFILE_ID, dry_run=False,
        bindings=DeviceBindings(shimmer_com_ports=["COM7"], camera_indices=[0]),
    )
    loaded = SessionConfig.load(cfg.save(tmp_path / "c.yaml"))
    assert loaded.dry_run is False


def test_from_dict_missing_dry_run_raises():
    with pytest.raises(ConfigError):
        SessionConfig.from_dict(
            {"participant": "p", "session": "s", "task": "t", "duration_s": 60, "out_dir": "x"}
        )


# -- #14/#19 malformed config -> ConfigError, not raw TypeError ------------
def test_from_dict_unknown_key_raises(tmp_path):
    d = _session(tmp_path).to_dict()
    d["mystery_future_field"] = 1
    with pytest.raises(ConfigError):
        SessionConfig.from_dict(d)


def test_from_dict_missing_required_raises():
    with pytest.raises(ConfigError):
        SessionConfig.from_dict({"dry_run": True})


# -- #3/#6/#20 validate() is a pure predicate -----------------------------
def test_validate_does_not_create_out_dir(tmp_path):
    target = tmp_path / "should_not_exist"
    cfg = _session(tmp_path)
    cfg.out_dir = target
    cfg.validate()
    assert not target.exists(), "validate() must not mutate the filesystem"


def test_validate_flags_uncreatable_out_dir(tmp_path):
    blocker = tmp_path / "a_file"
    blocker.write_text("x")
    cfg = _session(tmp_path)
    cfg.out_dir = blocker / "sub"  # parent is a file -> not creatable
    with pytest.raises(ConfigError) as exc:
        cfg.validate()
    assert "not creatable" in str(exc.value)


# -- #24 duration must be a real int --------------------------------------
@pytest.mark.parametrize("bad", [True, 60.0, "60"])
def test_non_int_duration_rejected(tmp_path, bad):
    cfg = _session(tmp_path)
    cfg.duration_s = bad
    with pytest.raises(ConfigError) as exc:
        cfg.validate()
    assert "int" in str(exc.value)


# -- #22 synthetic generators are deterministic ---------------------------
def test_synth_generators_are_deterministic():
    assert np.array_equal(synth_ecg(500, 256.0), synth_ecg(500, 256.0))
    assert np.array_equal(synth_audio_block(2000, 48000.0), synth_audio_block(2000, 48000.0))


# -- #11/#28 is_ready honours timeout + startup_delay ---------------------
def test_is_ready_honours_timeout(tmp_path):
    a = SimulatedKeyboard()
    a.startup_delay_s = 5.0  # device "warms up" longer than the timeout
    a.launch(_session(tmp_path))
    assert a.is_ready(0.05).ok is False
    a.startup_delay_s = 0.0
    assert a.is_ready(0.5).ok is True
    a.stop()


# -- #2 liveness gate does NOT lie when the outlet thread dies -------------
def test_liveness_unhealthy_when_outlet_thread_died(tmp_path):
    a = SimulatedShimmerEXG()
    a.launch(_session(tmp_path))
    # simulate "claimed an outlet, but its push thread died"
    a._lsl_active = True
    a._thread = None
    a._lsl_error = "boom"
    assert a._outlet_failed() is True
    rep = a.check_liveness(0.1)
    assert rep.ok is False
    assert any("died" in s.note for s in rep.streams)
    ready = a.is_ready(0.1)
    assert ready.ok is False and "failed" in ready.detail
    a.stop()


# -- #13 double launch() is idempotent (no orphan second thread) ----------
def test_double_launch_is_idempotent(tmp_path):
    a = SimulatedKeyboard()
    a.launch(_session(tmp_path))
    evt = a._stop_evt
    a.launch(_session(tmp_path))  # must be a no-op while running
    assert a._stop_evt is evt
    a.stop()
