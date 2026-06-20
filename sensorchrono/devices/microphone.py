"""Adapter for ``sensorchrono/bridges/audio_lsl_bridge.py`` (mic → Audio @ 48 kHz)."""
from __future__ import annotations

import re

from sensorchrono.devices.base import StreamDef
from sensorchrono.devices.bridge_adapter import BridgeAdapter, session_tag


class MicrophoneAdapter(BridgeAdapter):
    BRIDGE_MODULE = "sensorchrono.bridges.audio_lsl_bridge"

    def __init__(self, *,
                 device=None,
                 fleet_idx: int = 0,
                 **kw) -> None:
        super().__init__(**kw)
        self._device = device
        self._fleet_idx = fleet_idx
        self._stream_name = "Audio" if fleet_idx == 0 else f"Audio_{fleet_idx}"
        self.name = f"mic_{fleet_idx}"

    def _ready_pattern(self) -> re.Pattern[str]:
        return re.compile(rf"LSL outlet '{re.escape(self._stream_name)}' is live")

    def streams(self) -> list[StreamDef]:
        return [StreamDef(name=self._stream_name, content_type="Audio",
                          channels=1, nominal_rate_hz=48000.0)]

    def _tag(self, session) -> str:
        base = session_tag(session)
        return base if self._fleet_idx == 0 else f"{base}_mic{self._fleet_idx}"

    def _bridge_args(self, session) -> list[str]:
        dev = self._device
        if dev is None:
            devs = getattr(session.bindings, "mic_devices", [])
            if devs:
                dev = devs[min(self._fleet_idx, len(devs) - 1)]
        args = [
            "--duration", f"{self._duration(session):.0f}",
            "--out-dir", str(session.out_dir),
            "--tag", self._tag(session),
            "--stream-name", self._stream_name,
        ]
        if dev is not None:
            args += ["--device", str(dev)]
        return args
