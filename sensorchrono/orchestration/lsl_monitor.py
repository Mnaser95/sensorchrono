"""Live LSL liveness monitor — the staging gate's data source.

Resolves the expected streams by name on the LSL hub and, a few times a
second, measures each one's actual sample rate, largest inter-sample gap, and
channel count, comparing them to the contract's expectation. This is the
structural cure for the #1 historical failure (a stream silently missing or
under-rate in LabRecorder).

The verdict logic is a pure function (:func:`compute_stream_liveness`) so it is
unit-testable with no LSL; :class:`LslMonitor` is the threaded I/O wrapper that
feeds it (lazy ``pylsl`` import, so this module loads without liblsl).
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from sensorchrono.contract import STREAM_SPECS, StreamName, StreamSpec
from sensorchrono.devices.base import LivenessReport, StreamLiveness

#: a regular stream must sustain at least this fraction of its nominal rate
RATE_OK_FRACTION = 0.6
#: largest tolerated gap (s) between consecutive samples on a regular stream
GAP_LIMIT_S = 0.5
#: VideoFrames timestamps remain valid below the requested camera FPS. Require
#: forward progress, but report a warning rather than blocking on throughput.
MIN_VIDEO_RATE_HZ = 1.0


def compute_stream_liveness(
    spec: StreamSpec,
    *,
    present: bool,
    n_samples: int,
    window_s: float,
    max_gap_s: float,
    measured_channels: int,
) -> StreamLiveness:
    """Turn raw per-poll observations into a verdict for one stream.

    Markers (nominal rate 0) are judged on presence alone — they have no rate
    or gap to check. A ``measured_channels`` of 0 means "unknown" (don't fail
    on it); any positive value must equal the contract's expected channels."""
    expected_rate = spec.nominal_rate_hz
    is_marker = expected_rate == 0.0
    measured_rate = (n_samples / window_s) if window_s > 0 else 0.0

    if not present:
        return StreamLiveness(
            name=spec.name, present=False, measured_rate_hz=0.0,
            expected_rate_hz=expected_rate, max_gap_s=0.0, ok=False,
            measured_channels=0, expected_channels=spec.channels,
            note="stream not present on the LSL hub",
        )

    problems: list[str] = []
    notices: list[str] = []
    # expected channels=0 declares a dynamic layout (different EMOTIV models
    # expose different channel sets through Cortex).
    if spec.channels > 0 and measured_channels and measured_channels != spec.channels:
        problems.append(f"channels {measured_channels}!={spec.channels}")
    if not is_marker:
        # Rate-aware tolerances. The monitor polls in a short (~0.5 s) window, so
        # a fixed gap limit and rate floor are wrong for LOW-rate streams: the
        # 1 Hz ShimmerDiagnostics_ECG has ~1 s gaps *by definition* and shows 0
        # samples in most polls, so the old fixed 0.5 s gap limit + rate floor
        # made it permanently "unhealthy" — which kept the staging gate red and
        # the "Go to Recording" button disabled forever. Scale the gap limit to
        # the nominal period, and only enforce the rate floor when the window is
        # long enough to actually observe the nominal rate (≥ ~2 expected samples).
        period = 1.0 / expected_rate
        gap_limit = max(GAP_LIMIT_S, 3.0 * period)
        rate_floor = RATE_OK_FRACTION * expected_rate
        if spec.name == StreamName.VIDEO_FRAMES:
            if measured_rate < MIN_VIDEO_RATE_HZ:
                problems.append(
                    f"camera rate {measured_rate:.1f}<{MIN_VIDEO_RATE_HZ:.0f}Hz")
            elif measured_rate < rate_floor:
                notices.append(
                    f"low camera rate {measured_rate:.1f}/{expected_rate:.0f}Hz")
        elif window_s * expected_rate >= 2.0 and measured_rate < rate_floor:
            problems.append(
                f"rate {measured_rate:.1f}<{RATE_OK_FRACTION:.0%} "
                f"of {expected_rate:.0f}Hz")
        if max_gap_s > gap_limit:
            problems.append(f"gap {max_gap_s*1000:.0f}ms>{gap_limit*1000:.0f}ms")

    return StreamLiveness(
        name=spec.name,
        present=True,
        measured_rate_hz=measured_rate,
        expected_rate_hz=expected_rate,
        max_gap_s=max_gap_s,
        ok=not problems,
        measured_channels=measured_channels,
        expected_channels=spec.channels,
        note="; ".join([*problems, *notices]),
    )


class LslMonitor:
    """Background poller. Resolves the expected streams once, then each tick
    pulls whatever samples arrived and recomputes a :class:`LivenessReport`."""

    def __init__(
        self,
        expected: list[StreamName],
        *,
        poll_hz: float = 2.0,
        on_update: Callable[[LivenessReport], None] | None = None,
        device: str = "lsl",
    ) -> None:
        self.expected = list(expected)
        self.poll_dt = 1.0 / poll_hz
        self.on_update = on_update
        self.device = device
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._snapshot = LivenessReport(device=device, streams=())

    def snapshot(self) -> LivenessReport:
        with self._lock:
            return self._snapshot

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="lsl-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:  # pragma: no cover - exercised via venv integration test
        try:
            import pylsl
        except Exception:
            # No liblsl: report everything absent rather than crashing.
            self._publish_absent("pylsl unavailable")
            return

        inlets: dict[StreamName, object] = {}
        last_ts: dict[StreamName, float | None] = {n: None for n in self.expected}
        try:
            for name in self.expected:
                found = pylsl.resolve_byprop("name", str(name), 1, 1.0)
                if found:
                    inlets[name] = pylsl.StreamInlet(found[0], max_buflen=4)

            while not self._stop.wait(self.poll_dt):
                rows: list[StreamLiveness] = []
                for name in self.expected:
                    spec = STREAM_SPECS[name]
                    inlet = inlets.get(name)
                    if inlet is None:  # try to (re)resolve a late/return stream
                        found = pylsl.resolve_byprop("name", str(name), 1, 0.1)
                        if found:
                            inlets[name] = inlet = pylsl.StreamInlet(found[0], max_buflen=4)
                    if inlet is None:
                        rows.append(compute_stream_liveness(spec, present=False, n_samples=0, window_s=self.poll_dt, max_gap_s=0.0, measured_channels=0))
                        continue
                    # Drain the inlet fully: pull_chunk caps at ~1024 samples per
                    # call, which would massively under-count a 48 kHz stream and
                    # falsely fail its rate check. Loop until the buffer is empty.
                    all_stamps: list[float] = []
                    while True:
                        _, stamps = inlet.pull_chunk(timeout=0.0, max_samples=16384)
                        if not stamps:
                            break
                        all_stamps.extend(stamps)
                        if len(stamps) < 16384:
                            break
                    n = len(all_stamps)
                    gap = 0.0
                    if all_stamps:
                        prev = last_ts[name]
                        seq = ([prev] + all_stamps) if prev is not None else all_stamps
                        if len(seq) > 1:
                            gap = max(b - a for a, b in zip(seq, seq[1:]))
                        last_ts[name] = all_stamps[-1]
                    ch = inlet.info().channel_count()
                    rows.append(compute_stream_liveness(spec, present=True, n_samples=n, window_s=self.poll_dt, max_gap_s=gap, measured_channels=ch))
                self._set(LivenessReport(device=self.device, streams=tuple(rows)))
        except Exception as exc:
            # a monitor-thread crash must surface as a degraded report, not vanish
            self._publish_absent(f"monitor error: {exc!r}")
        finally:
            for inlet in inlets.values():
                try:
                    inlet.close_stream()
                except Exception:
                    pass

    def _publish_absent(self, note: str) -> None:  # pragma: no cover
        rows = [
            compute_stream_liveness(STREAM_SPECS[n], present=False, n_samples=0, window_s=self.poll_dt, max_gap_s=0.0, measured_channels=0)
            for n in self.expected
        ]
        self._set(LivenessReport(device=self.device, streams=tuple(rows)))

    def _set(self, report: LivenessReport) -> None:
        with self._lock:
            self._snapshot = report
        if self.on_update is not None:
            self.on_update(report)
