#!/usr/bin/env python3
"""Throughput benchmark for pydramsim3.

Measures transactions/second for the Python-loop ``replay()`` path vs the
zero-copy numpy ``run_trace()`` path, with and without callback collection.

Run::

    python benchmarks/benchmark.py
"""

from __future__ import annotations

import tempfile
import time

import numpy as np

from pydramsim3 import LatencyTracker, MemoryController

N = 100_000


def make_trace(n: int) -> tuple[np.ndarray, np.ndarray]:
    addrs = (0x1000 + np.arange(n) * 64).astype(np.uint64)
    writes = np.arange(n) % 2 == 1
    return addrs, writes


def bench(label: str, fn) -> None:
    t0 = time.perf_counter()
    cycles = fn()
    dt = time.perf_counter() - t0
    print(f"{label:22s} {N / dt / 1e3:8.0f} ktx/s  {dt * 1000:8.0f} ms  cycles={cycles}")


def main() -> None:
    addrs, writes = make_trace(N)
    with tempfile.TemporaryDirectory() as d:
        mc = MemoryController.from_config("DDR4_8Gb_x8_2400", working_dir=d)
        trace = [(int(a), bool(w)) for a, w in zip(addrs, writes)]
        bench("replay (Python loop)", lambda: mc.replay(trace))

        mc2 = MemoryController.from_config("DDR4_8Gb_x8_2400", working_dir=d)
        bench("run_trace (numpy)", lambda: mc2.run_trace(addrs, writes))

        tracker = LatencyTracker()
        mc3 = MemoryController.from_config(
            "DDR4_8Gb_x8_2400",
            working_dir=d,
            read_complete=tracker.on_read,
            write_complete=tracker.on_write,
        )
        bench(
            "run_trace + tracker",
            lambda: mc3.run_trace(addrs, writes),
        )
        assert tracker.num_reads + tracker.num_writes == N


if __name__ == "__main__":
    main()
