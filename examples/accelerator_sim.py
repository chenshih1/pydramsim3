#!/usr/bin/env python3
"""Example: simulating a hardware accelerator's memory traffic with DRAMsim3.

This script models a simplified matrix-multiply accelerator that loads
tiles of a weight matrix from DRAM, computes locally, and writes back
partial results.  It demonstrates:

  - Easy config discovery via ``from_config``
  - Context manager for clean lifecycle
  - Completion callbacks for integration with an outer simulator
  - Driving the simulator cycle-by-cycle from an outer loop
  - Using ``get_stats()`` for authoritative, structured result collection
"""

import tempfile
from collections import deque

import pydramsim3

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DRAM_CONFIG = "DDR4_8Gb_x8_2400"
TILE_SIZE = 256          # bytes per tile
NUM_TILES = 128          # number of tiles to load
BASE_ADDR = 0x1000_0000  # start address of the weight matrix in DRAM
COMPUTE_CYCLES = 20      # cycles the accelerator spends computing per tile

# ---------------------------------------------------------------------------
# Completion tracking via callbacks
# ---------------------------------------------------------------------------
# Callbacks are the integration hook: they tell the outer accelerator model
# "this data is now available".  For latency *statistics* we use DRAMsim3's
# internal JSON stats via get_stats().

_reads_completed = 0
_writes_completed = 0


def on_read_complete(addr: int) -> None:
    global _reads_completed
    _reads_completed += 1
    # In a real accelerator model you would signal the datapath here,
    # e.g. mark a tile as "data ready" or wake up a dependent operation.


def on_write_complete(addr: int) -> None:
    global _writes_completed
    _writes_completed += 1


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def _percentile_from_hist(dist: dict[str, int], pct: float) -> int:
    """Compute a percentile from a {latency_str: count} distribution."""
    items = sorted((int(k), v) for k, v in dist.items())
    total = sum(v for _, v in items)
    target = total * pct
    cumulative = 0
    for latency, count in items:
        cumulative += count
        if cumulative >= target:
            return latency
    return items[-1][0]


def report_stats(stats: dict) -> None:
    """Print DRAMsim3 stats in a research-friendly format."""
    ch0 = stats["0"]

    print("--- Latency (DRAMsim3 internal, cycles) ---")
    for label, key, done_key in [
        ("Read ", "read_latency", "num_reads_done"),
        ("Write", "write_latency", "num_writes_done"),
    ]:
        dist = {k: v for k, v in ch0[key].items() if k.isdigit()}
        if not dist:
            continue
        n = ch0[done_key]
        avg = sum(int(k) * v for k, v in dist.items()) / max(n, 1)
        p50 = _percentile_from_hist(dist, 0.50)
        p90 = _percentile_from_hist(dist, 0.90)
        p99 = _percentile_from_hist(dist, 0.99)
        lo = min(int(k) for k in dist)
        hi = max(int(k) for k in dist)
        print(f"  {label}  n={n}  avg={avg:.1f}  "
              f"min={lo}  p50={p50}  p90={p90}  p99={p99}  max={hi}")

    print()
    print("--- Energy & Power ---")
    print(f"  Total energy    : {ch0['total_energy']:.2f} pJ")
    print(f"  Average power   : {ch0['average_power']:.2f} mW")
    print(f"  Average bandwidth: {ch0['average_bandwidth']:.2f}")

    print()
    print("--- DRAM Efficiency ---")
    reads = ch0["num_reads_done"]
    writes = ch0["num_writes_done"]
    r_hits = ch0["num_read_row_hits"]
    w_hits = ch0["num_write_row_hits"]
    print(f"  Read  row-buffer hits : {r_hits}/{reads} ({100*r_hits/reads:.1f}%)")
    print(f"  Write row-buffer hits : {w_hits}/{writes} ({100*w_hits/writes:.1f}%)")
    print(f"  ACT commands          : {ch0['num_act_cmds']}")
    print(f"  PRE commands          : {ch0['num_pre_cmds']}")
    print(f"  Write buffer hits     : {ch0['num_write_buf_hits']}")


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

def run_simulation() -> None:
    configs = pydramsim3.list_configs()
    print(f"Available DRAM configs: {len(configs)}")
    print(f"Using: {DRAM_CONFIG}")
    print(f"Config dir: {pydramsim3.configs_dir()}")
    print()

    with tempfile.TemporaryDirectory(prefix="dramsim3_") as output_dir, \
         pydramsim3.MemorySystem.from_config(
             DRAM_CONFIG,
             working_dir=output_dir,
             read_callback=on_read_complete,
             write_callback=on_write_complete,
         ) as mem:

        print(f"Clock period : {mem.clock_period:.2f} ns")
        print(f"Queue size   : {mem.queue_size}")
        print(f"Burst size   : {mem.burst_size} bytes")
        print(f"Output dir   : {output_dir}")
        print()

        bursts_per_tile = TILE_SIZE // mem.burst_size
        addr_stride = mem.burst_size

        def tile_base_addr(tile_idx: int) -> int:
            return BASE_ADDR + tile_idx * 4096

        # Build request queue: reads first, then writes
        request_queue: deque[tuple[int, bool, int]] = deque()

        for tile in range(NUM_TILES):
            base = tile_base_addr(tile)
            for b in range(bursts_per_tile):
                request_queue.append((base + b * addr_stride, False, 0))

        write_start_cycle = NUM_TILES * (bursts_per_tile + COMPUTE_CYCLES)
        for tile in range(NUM_TILES):
            base = tile_base_addr(tile) + 0x8000_0000
            for b in range(bursts_per_tile):
                request_queue.append((base + b * addr_stride, True, write_start_cycle))

        total_requests = len(request_queue)
        print(f"Simulating {NUM_TILES} tiles "
              f"({bursts_per_tile} bursts/tile, {TILE_SIZE} B/tile) ...")
        print(f"Total memory requests: {total_requests}")
        print()

        # Main loop — cycle-accurate drive
        cycle = 0
        max_cycles = 500_000

        while cycle < max_cycles:
            while request_queue and request_queue[0][2] <= cycle:
                addr, is_write, _ = request_queue[0]
                if mem.can_accept(addr, is_write):
                    request_queue.popleft()
                    mem.enqueue(addr, is_write)
                else:
                    break

            cycle += 1
            mem.tick()

            if _reads_completed + _writes_completed == total_requests:
                break

        print(f"Simulation finished at cycle {cycle}")
        print(f"  Reads completed  : {_reads_completed}")
        print(f"  Writes completed : {_writes_completed}")
        print()

        # Collect authoritative stats via get_stats()
        stats = mem.get_stats()
        report_stats(stats)


if __name__ == "__main__":
    run_simulation()
