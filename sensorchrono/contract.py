"""Canonical LSL stream-name contract — the single source of truth.

Both tiers of this project reference LSL stream names as bare strings: the
real-time bridges (producers, repo root) and the analysis modules (consumers,
``analysis/``). Renaming a stream in a bridge silently breaks downstream
analysis, so the names live here *once* and everything imports them.

This module is intentionally dependency-free (stdlib only) so it can be
imported from anywhere — bridges, analysis, orchestration, GUI — without
pulling in ``pylsl``, Qt, or numpy.

Ground truth for the names was verified against the actual bridges and
``analysis/`` modules; they match producer↔consumer exactly today.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class StreamName(StrEnum):
    """Canonical LSL stream names.

    Members are real ``str`` (``StrEnum``), so ``StreamName.AUDIO == "Audio"``
    is true and a member works anywhere a plain string name is expected —
    ``pyxdf`` lookups, ``pylsl.resolve_byprop("name", ...)``, dict keys, etc.
    """

    SHIMMER_ECG = "ShimmerECG"
    # ECG and EMG are *mutually exclusive* bridge modes (shimmer_lsl_bridge runs
    # run_ecg() OR run_emg() per the profile's `mode:`), never co-resident — the
    # Phase-2 Shimmer adapter must pick its streams from the resolved mode.
    SHIMMER_EMG = "ShimmerEMG"
    SHIMMER_MARKERS = "ShimmerMarkers"
    SHIMMER_DIAGNOSTICS_ECG = "ShimmerDiagnostics_ECG"
    SHIMMER_ACCEL = "ShimmerAccel"  # future (accel adapter); plugin seam ready
    EMOTIV_EEG = "EmotivEEG"
    EMOTIV_MOTION = "EmotivMotion"
    AUDIO = "Audio"
    VIDEO_FRAMES = "VideoFrames"
    KEYBOARD_FIDUCIAL = "KeyboardFiducial"


@dataclass(frozen=True, slots=True)
class StreamSpec:
    """The *expected* shape of a canonical stream, from known hardware / the
    device profile. Used to validate liveness: an adapter that claims to emit
    ``ShimmerECG`` should produce ~256 Hz on 4 channels. ``nominal_rate_hz``
    of ``0.0`` marks an irregular / marker stream (no fixed rate)."""

    name: StreamName
    content_type: str
    channels: int
    nominal_rate_hz: float


# Expected specs per canonical stream. Rates/channels mirror
# profiles/shimmer3_exg_sr47-5-1.yaml and the BRIO defaults; treat them as
# nominal — the actual session rate (e.g. camera --fps) can override.
STREAM_SPECS: dict[StreamName, StreamSpec] = {
    StreamName.SHIMMER_ECG: StreamSpec(StreamName.SHIMMER_ECG, "ECG", 4, 256.0),
    StreamName.SHIMMER_EMG: StreamSpec(StreamName.SHIMMER_EMG, "EMG", 3, 512.0),
    StreamName.SHIMMER_MARKERS: StreamSpec(StreamName.SHIMMER_MARKERS, "Markers", 1, 0.0),
    StreamName.SHIMMER_DIAGNOSTICS_ECG: StreamSpec(
        StreamName.SHIMMER_DIAGNOSTICS_ECG, "Diagnostics", 5, 1.0
    ),
    StreamName.SHIMMER_ACCEL: StreamSpec(StreamName.SHIMMER_ACCEL, "Accel", 3, 256.0),
    # Cortex discovers channel layouts at subscription time. channels=0 means
    # "dynamic" to the liveness monitor, not a zero-channel LSL outlet.
    StreamName.EMOTIV_EEG: StreamSpec(StreamName.EMOTIV_EEG, "EEG", 0, 128.0),
    StreamName.EMOTIV_MOTION: StreamSpec(StreamName.EMOTIV_MOTION, "Motion", 0, 0.0),
    StreamName.AUDIO: StreamSpec(StreamName.AUDIO, "Audio", 1, 48000.0),
    # VideoFrames carries [frame_idx, cap_pos_ms] -> 2 channels, matching
    # video_lsl_bridge.py (channel_count=2) and profiles/logitech_brio.yaml.
    StreamName.VIDEO_FRAMES: StreamSpec(StreamName.VIDEO_FRAMES, "VideoFrames", 2, 30.0),
    StreamName.KEYBOARD_FIDUCIAL: StreamSpec(StreamName.KEYBOARD_FIDUCIAL, "Markers", 1, 0.0),
}

# Streams the analysis tier (analysis/postprocess.py et al.) actually consumes.
# Verified by searching analysis/ for each literal name. ShimmerEMG / Markers /
# Accel are recorded to the XDF but have no Tier-2 consumer today.
ANALYSIS_CONSUMED: frozenset[StreamName] = frozenset(
    {
        StreamName.SHIMMER_ECG,
        StreamName.SHIMMER_DIAGNOSTICS_ECG,
        StreamName.EMOTIV_EEG,
        StreamName.EMOTIV_MOTION,
        StreamName.AUDIO,
        StreamName.VIDEO_FRAMES,
        StreamName.KEYBOARD_FIDUCIAL,
    }
)


def spec(name: str | StreamName) -> StreamSpec:
    """Expected :class:`StreamSpec` for a canonical stream name.

    Accepts either a :class:`StreamName` or a plain string (validated through
    the enum, so an unknown name raises ``ValueError`` early rather than
    silently mismatching downstream)."""
    return STREAM_SPECS[StreamName(name)]


def indexed_stream_name(base: StreamName, idx: int) -> str:
    """LSL stream name for the device at fleet index ``idx``.

    Index 0 keeps the canonical name for backward compatibility with existing
    XDFs and analysis code. Higher indices append ``_<idx>`` so multiple
    devices of the same type can coexist on one LSL network.

    Examples::

        indexed_stream_name(StreamName.SHIMMER_ECG, 0) == "ShimmerECG"
        indexed_stream_name(StreamName.SHIMMER_ECG, 1) == "ShimmerECG_1"
        indexed_stream_name(StreamName.VIDEO_FRAMES, 2) == "VideoFrames_2"
    """
    if idx == 0:
        return str(base)
    return f"{base}_{idx}"
