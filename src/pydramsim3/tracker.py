# -*- coding: utf-8 -*-
"""Latency collection and percentile reporting for MemoryController."""

from __future__ import annotations

from typing import Optional

__all__ = ["LatencyTracker"]


class LatencyStats:
    """Summary statistics for a collected latency series."""

    __slots__ = ("_data",)

    def __init__(self, data: list[int]) -> None:
        self._data = sorted(data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        if not self._data:
            return "LatencyStats(n=0)"
        return (
            f"LatencyStats(n={len(self)}, avg={self.avg:.1f}, "
            f"p50={self.p50}, p99={self.p99}, max={self.max})"
        )

    @property
    def count(self) -> int:
        return len(self._data)

    @property
    def avg(self) -> float:
        if not self._data:
            return 0.0
        return sum(self._data) / len(self._data)

    @property
    def min(self) -> int:
        return self._data[0] if self._data else 0

    @property
    def max(self) -> int:
        return self._data[-1] if self._data else 0

    @property
    def p50(self) -> int:
        return self._percentile(0.50)

    @property
    def p90(self) -> int:
        return self._percentile(0.90)

    @property
    def p95(self) -> int:
        return self._percentile(0.95)

    @property
    def p99(self) -> int:
        return self._percentile(0.99)

    def percentile(self, pct: float) -> int:
        """Arbitrary percentile (0.0–1.0)."""
        return self._percentile(pct)

    def _percentile(self, pct: float) -> int:
        if not self._data:
            return 0
        idx = int(len(self._data) * pct)
        return self._data[min(idx, len(self._data) - 1)]

    @property
    def values(self) -> list[int]:
        """Sorted copy of all recorded latencies."""
        return list(self._data)


class LatencyTracker:
    """Collects per-transaction latencies from MemoryController callbacks.

    Usage::

        tracker = LatencyTracker()
        mc = MemoryController.from_config(
            "DDR4_8Gb_x8_2400",
            read_complete=tracker.on_read,
            write_complete=tracker.on_write,
        )
        mc.replay(trace)

        print(tracker.read_stats.avg)
        print(tracker.write_stats.p99)
    """

    def __init__(self) -> None:
        self._read_latencies: list[int] = []
        self._write_latencies: list[int] = []

    def on_read(self, addr: int, latency: int) -> None:
        """Callback for ``MemoryController(read_complete=...)``."""
        self._read_latencies.append(latency)

    def on_write(self, addr: int, latency: int) -> None:
        """Callback for ``MemoryController(write_complete=...)``."""
        self._write_latencies.append(latency)

    @property
    def read_stats(self) -> LatencyStats:
        return LatencyStats(self._read_latencies)

    @property
    def write_stats(self) -> LatencyStats:
        return LatencyStats(self._write_latencies)

    @property
    def all_stats(self) -> LatencyStats:
        """Combined read + write latencies."""
        return LatencyStats(self._read_latencies + self._write_latencies)

    @property
    def num_reads(self) -> int:
        return len(self._read_latencies)

    @property
    def num_writes(self) -> int:
        return len(self._write_latencies)

    def reset(self) -> None:
        """Clear all collected latencies."""
        self._read_latencies.clear()
        self._write_latencies.clear()

    def summary(self) -> str:
        """One-line summary suitable for logging."""
        parts = []
        if self._read_latencies:
            r = self.read_stats
            parts.append(f"read(n={r.count}, avg={r.avg:.1f}, p99={r.p99})")
        if self._write_latencies:
            w = self.write_stats
            parts.append(f"write(n={w.count}, avg={w.avg:.1f}, p99={w.p99})")
        return " ".join(parts) if parts else "no transactions"
