#!/usr/bin/env python3
"""Example: simulating a hardware accelerator's memory traffic with DRAMsim3.

This script models a simplified matrix-multiply accelerator that loads
tiles of a weight matrix from DRAM, computes locally, and writes back
partial results.  It demonstrates:

  - MemoryController with gem5-style flow control (submit / backpressure / retry)
  - LatencyTracker for percentile reporting
  - replay() for trace-driven simulation with automatic backpressure
  - logging for progress visibility (set level to DEBUG for backpressure detail)
  - Using ``get_stats()`` for authoritative DRAMsim3 internal stats
"""

import logging
import tempfile

import pydramsim3

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DRAM_CONFIG = "DDR4_8Gb_x8_2400"
TILE_SIZE = 256          # bytes per tile
NUM_TILES = 128          # number of tiles to load
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


def report_dramsim3_stats(stats: dict) -> None:
    """Print DRAMsim3 internal stats."""
    ch0 = stats["0"]
    logging.info("DRAMsim3: reads=%d writes=%d avg_read_lat=%.1f energy=%.0f pJ",
                 ch0["num_reads_done"], ch0["num_writes_done"],
                 ch0["average_read_latency"], ch0["total_energy"])
    reads = ch0["num_reads_done"]
    if reads:
        logging.info("DRAMsim3: read row hits %d/%d (%.1f%%)",
                     ch0["num_read_row_hits"], reads,
                     100 * ch0["num_read_row_hits"] / reads)


def run_simulation() -> None:
    logging.info("Config: %s, tiles: %d, tile size: %d B", DRAM_CONFIG, NUM_TILES, TILE_SIZE)

    tracker = pydramsim3.LatencyTracker()

    with tempfile.TemporaryDirectory(prefix="dramsim3_") as output_dir, \
         pydramsim3.MemoryController.from_config(
             DRAM_CONFIG,
             working_dir=output_dir,
             read_complete=tracker.on_read,
             write_complete=tracker.on_write,
         ) as mc:

        logging.info("Clock: %.2f ns, queue: %d, burst: %d B",
                     mc.clock_period, mc.queue_size, mc.burst_size)

        trace = build_trace(mc.burst_size)
        logging.info("Trace: %d transactions (%d reads + %d writes)",
                     len(trace), NUM_TILES * (TILE_SIZE // mc.burst_size),
                     NUM_TILES * (TILE_SIZE // mc.burst_size))

        total_cycles = mc.replay(trace)

        logging.info("Done: %d cycles, %s", total_cycles, tracker.summary())
        logging.info("Read  latency: avg=%.1f p50=%d p90=%d p99=%d max=%d",
                     tracker.read_stats.avg, tracker.read_stats.p50,
                     tracker.read_stats.p90, tracker.read_stats.p99,
                     tracker.read_stats.max)
        logging.info("Write latency: avg=%.1f p50=%d p99=%d",
                     tracker.write_stats.avg, tracker.write_stats.p50,
                     tracker.write_stats.p99)

        report_dramsim3_stats(mc.get_stats())


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    run_simulation()
