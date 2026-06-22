"""Adapter for an EMOTIV headset exposed by EMOTIV Launcher's Cortex API."""
from __future__ import annotations

import re
from pathlib import Path

from sensorchrono.contract import StreamName
from sensorchrono.devices.base import StreamDef
from sensorchrono.devices.bridge_adapter import BridgeAdapter, session_tag


class EmotivAdapter(BridgeAdapter):
    BRIDGE_MODULE = "sensorchrono.bridges.emotiv_lsl_bridge"

    def __init__(self, *, headset_id: str | None = None,
                 credentials_file: str | None = None,
                 streams: list[str] | None = None, **kw) -> None:
        super().__init__(**kw)
        self.headset_id = headset_id or None
        self.credentials_file = credentials_file or None
        requested = streams or ["eeg", "mot"]
        self.requested_streams = [s for s in requested if s in {"eeg", "mot"}]
        if not self.requested_streams:
            raise ValueError("EMOTIV needs at least one of: eeg, mot")
        self.name = "emotiv"

    def _ready_pattern(self) -> re.Pattern[str]:
        first = "EmotivEEG" if "eeg" in self.requested_streams else "EmotivMotion"
        return re.compile(rf"LSL outlet '{first}' is live")

    def streams(self) -> list[StreamDef]:
        out: list[StreamDef] = []
        if "eeg" in self.requested_streams:
            out.append(StreamDef(StreamName.EMOTIV_EEG, "EEG", 0, 128.0))
        if "mot" in self.requested_streams:
            out.append(StreamDef(StreamName.EMOTIV_MOTION, "Motion", 0, 0.0))
        return out

    def _stop_file(self, session) -> Path:
        return Path(session.out_dir) / f"{session_tag(session)}_emotiv.stop"

    def _bridge_args(self, session) -> list[str]:
        args = [
            "--streams", ",".join(self.requested_streams),
            "--duration", f"{self._duration(session):.0f}",
            "--stop-file", str(self._stop_file(session)),
            "--eeg-stream-name", str(StreamName.EMOTIV_EEG),
            "--motion-stream-name", str(StreamName.EMOTIV_MOTION),
        ]
        if self.headset_id:
            args += ["--headset-id", self.headset_id]
        if self.credentials_file:
            args += ["--credentials-file", self.credentials_file]
        return args

