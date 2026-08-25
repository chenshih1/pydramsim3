#!/usr/bin/env python3
"""Throughput of ``Memory.replay`` vs ``Memory.run_trace``.

Run ``python benchmarks/benchmark.py``.
"""

from __future__ import annotations

import tempfile
import time

import numpy as np

from pydramsim3 import LatencyTracker, Memory

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
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=d)
        trace = [(int(a), bool(w)) for a, w in zip(addrs, writes)]
        bench("replay (Python loop)", lambda: mem.replay(trace))

        mem2 = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=d)
        bench("run_trace (numpy)", lambda: mem2.run_trace(addrs, writes))

        tracker = LatencyTracker()
        mem3 = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=d)

        def run_traced():
            cycles = mem3.run_trace(addrs, writes)
            tracker.add(mem3.pull())
            return cycles

        bench("run_trace + tracker", run_traced)
        assert tracker.num_reads + tracker.num_writes == N


if __name__ == "__main__":
    main()
