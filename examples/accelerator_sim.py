#!/usr/bin/env python3
"""Example: simulating a hardware accelerator's memory traffic with DRAMsim3.

Demonstrates the event-driven ``Memory`` API: submit a trace, then ``drain``
and read ``Completion.latency`` / ``Completion.cycle``.
"""

from __future__ import annotations

import logging
import tempfile

import pydramsim3

logger = logging.getLogger("accelerator_sim")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DRAM_CONFIG = "DDR4_8Gb_x8_2400"
TILE_SIZE = 256  # bytes per tile
NUM_TILES = 128  # number of tiles to load
BASE_ADDR = 0x1000_0000  # start address of the weight matrix in DRAM

# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------


def build_trace(burst_size: int) -> list[tuple[int, bool]]:
    """Build the memory trace: tile reads followed by result writes."""
    bursts_per_tile = TILE_SIZE // burst_size
    trace: list[tuple[int, bool]] = []

    for tile in range(NUM_TILES):
        base = BASE_ADDR + tile * 4096
        for b in range(bursts_per_tile):
            trace.append((base + b * burst_size, False))

    for tile in range(NUM_TILES):
        base = BASE_ADDR + tile * 4096 + 0x8000_0000
        for b in range(bursts_per_tile):
            trace.append((base + b * burst_size, True))

    return trace


def run_simulation() -> None:
    logger.info("Config: %s, tiles: %d, tile size: %d B", DRAM_CONFIG, NUM_TILES, TILE_SIZE)

    with tempfile.TemporaryDirectory(prefix="dramsim3_") as output_dir:
        mem = pydramsim3.Memory.from_config(DRAM_CONFIG, working_dir=output_dir)
        logger.info(
            "Clock: %.2f ns, queue: %d, burst: %d B",
            mem.clock_period,
            mem.queue_size,
            mem.burst_size,
        )

        trace = build_trace(mem.burst_size)
        logger.info(
            "Trace: %d transactions (%d reads + %d writes)",
            len(trace),
            NUM_TILES * (TILE_SIZE // mem.burst_size),
            NUM_TILES * (TILE_SIZE // mem.burst_size),
        )

        for addr, is_write in trace:
            mem.submit(addr, is_write)
        evs = mem.drain()
        reads = [e.latency for e in evs if not e.is_write]
        writes = [e.latency for e in evs if e.is_write]

        logger.info("Done: %d cycles, %d completions", mem.current_cycle, len(evs))
        if reads:
            logger.info(
                "Read  latency: avg=%.1f min=%d max=%d",
                sum(reads) / len(reads),
                min(reads),
                max(reads),
            )
        if writes:
            logger.info(
                "Write latency: avg=%.1f min=%d max=%d",
                sum(writes) / len(writes),
                min(writes),
                max(writes),
            )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    run_simulation()
