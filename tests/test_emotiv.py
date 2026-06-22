"""Hardware-free coverage for the EMOTIV Cortex integration."""
from __future__ import annotations

from sensorchrono.bridges.emotiv_lsl_bridge import (
    read_credentials,
    selected_indices,
)
from sensorchrono.config import DeviceBindings, SessionConfig
from sensorchrono.contract import STREAM_SPECS, StreamName
from sensorchrono.devices.bridge_adapter import build_real_fleet
from sensorchrono.devices.emotiv import EmotivAdapter
from sensorchrono.orchestration.lsl_monitor import compute_stream_liveness


def session(tmp_path, bindings: DeviceBindings) -> SessionConfig:
    return SessionConfig(
        participant="p01", session="s1", task="rest", duration_s=30,
        out_dir=tmp_path / "out", dry_run=False, bindings=bindings,
    )


def test_credentials_file_formats(tmp_path, monkeypatch):
    monkeypatch.delenv("EMOTIV_CLIENT_ID", raising=False)
    monkeypatch.delenv("EMOTIV_CLIENT_SECRET", raising=False)
    path = tmp_path / "credentials.txt"
    path.write_text("CLIENT_ID=abc\nCLIENT_SECRET: xyz\n", encoding="utf-8")
    assert read_credentials(str(path)) == ("abc", "xyz")


def test_environment_credentials_override_file(tmp_path, monkeypatch):
    path = tmp_path / "credentials.txt"
    path.write_text("file-id\nfile-secret\n", encoding="utf-8")
    monkeypatch.setenv("EMOTIV_CLIENT_ID", "env-id")
    monkeypatch.setenv("EMOTIV_CLIENT_SECRET", "env-secret")
    assert read_credentials(str(path)) == ("env-id", "env-secret")


def test_eeg_auxiliary_columns_are_not_published():
    columns = ["COUNTER", "AF3", "T7", "Pz", "RAW_CQ", "MARKERS"]
    assert selected_indices("eeg", columns) == [1, 2, 3]
    assert selected_indices("mot", ["Q0", "Q1"]) == [0, 1]


def test_adapter_declares_and_builds_emotiv_streams(tmp_path):
    cred = tmp_path / "credentials.txt"
    cred.write_text("id\nsecret\n", encoding="utf-8")
    adapter = EmotivAdapter(
        headset_id="INSIGHT2-1234", credentials_file=str(cred))
    assert {s.name for s in adapter.streams()} == {
        StreamName.EMOTIV_EEG, StreamName.EMOTIV_MOTION,
    }
    argv = adapter.build_argv(session(
        tmp_path,
        DeviceBindings(shimmer_com_ports=["COM3"], camera_indices=[0],
                       emotiv_enabled=True, emotiv_credentials_file=str(cred)),
    ))
    assert argv[:3] == [
        __import__("sys").executable, "-m",
        "sensorchrono.bridges.emotiv_lsl_bridge",
    ]
    assert argv[argv.index("--headset-id") + 1] == "INSIGHT2-1234"
    assert argv[argv.index("--credentials-file") + 1] == str(cred)


def test_real_fleet_includes_emotiv(tmp_path):
    cred = tmp_path / "credentials.txt"
    cred.write_text("id\nsecret\n", encoding="utf-8")
    cfg = session(
        tmp_path,
        DeviceBindings(shimmer_com_ports=["COM3"], camera_indices=[0],
                       emotiv_enabled=True, emotiv_credentials_file=str(cred)),
    )
    assert "emotiv" in {adapter.name for adapter in build_real_fleet(cfg)}


def test_dynamic_emotiv_channel_count_is_accepted():
    spec = STREAM_SPECS[StreamName.EMOTIV_EEG]
    result = compute_stream_liveness(
        spec, present=True, n_samples=128, window_s=1.0,
        max_gap_s=0.01, measured_channels=14,
    )
    assert result.ok, result.note


def test_postprocess_exports_emotiv_receipt_timestamps(tmp_path):
    import numpy as np
    from analysis.postprocess import _write_unified_outputs

    by_name = {
        "EmotivEEG": {
            "time_stamps": np.asarray([10.0, 10.01]),
            "time_series": np.asarray([[1.0, 2.0], [3.0, 4.0]]),
        }
    }
    written = _write_unified_outputs(
        tmp_path, by_name, ecg_corrected_ts=None, lag_ms={})
    output = tmp_path / "emotiv_eeg.csv"
    assert output in written
    lines = output.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "lsl_receipt_s,ch_0,ch_1"
    assert lines[1].startswith("10.0,1.0,2.0")

