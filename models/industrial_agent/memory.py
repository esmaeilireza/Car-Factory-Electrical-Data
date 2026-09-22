"""
Rolling memory primitives for trend detection.
"""

from __future__ import annotations

import math
import time
from collections import deque
from typing import Deque, Optional, Tuple


class MetricWindow:
    """
    Rolling numeric window for one metric of one equipment.
    Supports latest, mean, std, slope, age.
    """

    def __init__(self, maxlen: int = 300):
        self.maxlen = maxlen
        self.values: Deque[Tuple[float, float]] = deque(maxlen=maxlen)

    def update(self, ts: float, value: float) -> None:
        if value is None:
            return
        try:
            v = float(value)
            if math.isnan(v) or math.isinf(v):
                return
        except Exception:
            return
        self.values.append((ts, v))

    @property
    def count(self) -> int:
        return len(self.values)

    @property
    def latest(self) -> Optional[float]:
        if not self.values:
            return None
        return self.values[-1][1]

    @property
    def age_sec(self) -> Optional[float]:
        if not self.values:
            return None
        return time.time() - self.values[-1][0]

    @property
    def mean(self) -> Optional[float]:
        if not self.values:
            return None
        return sum(v for _, v in self.values) / len(self.values)

    @property
    def std(self) -> Optional[float]:
        if len(self.values) < 2:
            return 0.0
        m = self.mean or 0.0
        var = sum((v - m) ** 2 for _, v in self.values) / (len(self.values) - 1)
        return math.sqrt(var)

    @property
    def slope_per_min(self) -> Optional[float]:
        """
        Simple linear slope in units per minute.
        """
        if len(self.values) < 2:
            return 0.0

        t0 = self.values[0][0]
        xs = [t - t0 for t, _ in self.values]
        ys = [v for _, v in self.values]

        n = len(xs)
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n

        cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        var_x = sum((x - mean_x) ** 2 for x in xs)

        if var_x <= 1e-9:
            return 0.0

        slope_per_sec = cov / var_x
        return slope_per_sec * 60.0

    def zscore(self, value: Optional[float] = None) -> Optional[float]:
        if value is None:
            value = self.latest
        if value is None:
            return None
        m = self.mean
        s = self.std
        if m is None or s is None or s <= 1e-9:
            return 0.0
        return (value - m) / s
