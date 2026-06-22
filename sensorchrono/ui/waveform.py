"""Live ECG trace (pyqtgraph ring buffer) + a simple audio level meter.

Performance per the plan: ``setDownsampling(auto=True)`` + ``setClipToView``
so a long ring buffer of high-rate ECG stays cheap to redraw, and we replace
the whole buffer with one ``setData`` per update rather than appending points.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6 import QtWidgets


class WaveformWidget(pg.PlotWidget):
    """Fixed-length ring buffer plotted as a scrolling signal trace."""

    def __init__(self, *, buffer_n: int = 2560,
                 y_range: tuple[float, float] = (-1.5, 1.5),
                 y_label: str = "ECG (mV)", parent=None) -> None:
        super().__init__(parent)
        self.buffer_n = buffer_n
        self._buf = np.zeros(buffer_n, dtype=float)
        self._curve = self.plot(self._buf, pen=pg.mkPen((0, 220, 120), width=1))
        self.setYRange(*y_range)
        self.setMouseEnabled(x=False, y=False)
        self.hideButtons()
        plot = self.getPlotItem()
        plot.setDownsampling(auto=True)
        plot.setClipToView(True)
        plot.setMenuEnabled(False)
        self.setLabel("left", y_label)
        self.hideAxis("bottom")

    def set_signal_name(self, name: str) -> None:
        self.setTitle(str(name))

    def append(self, samples) -> None:
        s = np.asarray(samples, dtype=float).ravel()
        n = s.size
        if n == 0:
            return
        if n >= self.buffer_n:
            self._buf = s[-self.buffer_n:].astype(float, copy=True)
        else:
            self._buf = np.roll(self._buf, -n)
            self._buf[-n:] = s
        self._curve.setData(self._buf)

    def clear_buffer(self) -> None:
        self._buf[:] = 0.0
        self._curve.setData(self._buf)


class AudioLevelMeter(QtWidgets.QProgressBar):
    """RMS-driven horizontal level bar (0..1 → 0..100%)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setRange(0, 100)
        self.setTextVisible(True)
        self.setFormat("mic %p%")

    def set_level(self, rms: float) -> None:
        self.setValue(max(0, min(100, int(float(rms) * 100))))
