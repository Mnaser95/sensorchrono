"""Adapter for ``sensorchrono/bridges/shimmer_lsl_bridge.py`` (Shimmer3 EXG: ECG or EMG).

Two deadlock traps the adapter MUST avoid for a headless run:
  1. the bridge blocks on ``input()`` unless ``--no-prompt`` is passed, and
  2. the positional ``mode`` argument prompts interactively if omitted.
Both are enforced here and covered by a test.
"""
from __future__ import annotations

import re

from sensorchrono.contract import StreamName, indexed_stream_name
from sensorchrono.devices.base import StreamDef
from sensorchrono.devices.bridge_adapter import BridgeAdapter


class ShimmerExgAdapter(BridgeAdapter):
    BRIDGE_MODULE = "sensorchrono.bridges.shimmer_lsl_bridge"

    def __init__(self, *,
                 com_port: str | None = None,
                 fleet_idx: int = 0,
                 mode: str = "ecg",
                 start_delay_s: float = 3.0,
                 **kw) -> None:
        super().__init__(**kw)
        if mode not in ("ecg", "emg"):
            raise ValueError(f"unsupported shimmer mode {mode!r} (v1 supports ecg|emg)")
        self._com_port = com_port
        self._fleet_idx = fleet_idx
        self.mode = mode
        self.start_delay_s = start_delay_s
        self.name = f"shimmer_exg_{fleet_idx}"

    def _ready_pattern(self) -> re.Pattern[str]:
        base = "ShimmerEMG" if self.mode == "emg" else "ShimmerECG"
        suffix = "" if self._fleet_idx == 0 else f"_{self._fleet_idx}"
        return re.compile(rf"LSL outlet: {base}{suffix}")

    def streams(self) -> list[StreamDef]:
        suffix = "" if self._fleet_idx == 0 else f"_{self._fleet_idx}"
        if self.mode == "emg":
            return [StreamDef(
                name=f"ShimmerEMG{suffix}",
                content_type="EMG", channels=3, nominal_rate_hz=512.0)]
        return [
            StreamDef(name=f"ShimmerECG{suffix}",
                      content_type="ECG", channels=4, nominal_rate_hz=256.0),
            StreamDef(name=f"ShimmerDiagnostics_ECG{suffix}",
                      content_type="Diagnostics", channels=5, nominal_rate_hz=1.0),
        ]

    def _bridge_args(self, session) -> list[str]:
        # Explicit com_port wins; fall back to session bindings for legacy single-device use.
        port = self._com_port
        if not port:
            ports = getattr(session.bindings, "shimmer_com_ports", [])
            if ports:
                port = ports[min(self._fleet_idx, len(ports) - 1)]
        suffix = "" if self._fleet_idx == 0 else f"_{self._fleet_idx}"
        args = [
            self.mode,
            "--no-prompt",
            "--record-seconds", f"{self._duration(session):.0f}",
            "--start-delay", f"{self.start_delay_s:g}",
            "--stream-suffix", suffix,
        ]
        if port:
            flag = "--emg-port" if self.mode == "emg" else "--ecg-port"
            args += [flag, str(port)]
        return args
