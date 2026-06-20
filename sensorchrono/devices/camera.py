"""Adapter for ``sensorchrono/bridges/video_lsl_bridge.py`` (UVC camera → VideoFrames + .mp4)."""
from __future__ import annotations

import re
from pathlib import Path

from sensorchrono.devices.base import StreamDef
from sensorchrono.devices.bridge_adapter import BridgeAdapter, session_tag


class CameraAdapter(BridgeAdapter):
    BRIDGE_MODULE = "sensorchrono.bridges.video_lsl_bridge"

    def __init__(self, *,
                 camera_device_idx: int | None = None,
                 fleet_idx: int = 0,
                 **kw) -> None:
        super().__init__(**kw)
        self._camera_device_idx = camera_device_idx
        self._fleet_idx = fleet_idx
        self._stream_name = "VideoFrames" if fleet_idx == 0 else f"VideoFrames_{fleet_idx}"
        self.name = f"camera_{fleet_idx}"

    def _ready_pattern(self) -> re.Pattern[str]:
        return re.compile(rf"LSL outlet '{re.escape(self._stream_name)}' is live")

    def streams(self) -> list[StreamDef]:
        return [StreamDef(name=self._stream_name, content_type="VideoFrames",
                          channels=2, nominal_rate_hz=30.0)]

    def _tag(self, session) -> str:
        base = session_tag(session)
        return base if self._fleet_idx == 0 else f"{base}_cam{self._fleet_idx}"

    def _stop_file(self, session) -> Path:
        return Path(session.out_dir) / f"{self._tag(session)}_video.stop"

    def _bridge_args(self, session) -> list[str]:
        cam_idx = self._camera_device_idx
        if cam_idx is None:
            idxs = getattr(session.bindings, "camera_indices", [])
            if idxs:
                cam_idx = idxs[min(self._fleet_idx, len(idxs) - 1)]
        args = [
            "--duration", f"{self._duration(session):.0f}",
            "--out-dir", str(session.out_dir),
            "--tag", self._tag(session),
            "--stop-file", str(self._stop_file(session)),
            "--stream-name", self._stream_name,
            "--preview-path", str(self.preview_path(session)),
            "--preview-fps", "2",
        ]
        if cam_idx is not None:
            args += ["--device", str(cam_idx)]
        return args

    def mp4_path(self, session) -> Path:
        return Path(session.out_dir) / f"{self._tag(session)}_video.mp4"

    def preview_path(self, session) -> Path:
        return Path(session.out_dir) / f"{self._tag(session)}_preview.jpg"
