# PyDRAMsim3

[![CI](https://img.shields.io/github/actions/workflow/status/chenshih1/pydramsim3/ci.yml?branch=master&label=CI&logo=github)](https://github.com/chenshih1/pydramsim3/actions)
[![Release](https://img.shields.io/github/v/release/chenshih1/pydramsim3?label=release&logo=github)](https://github.com/chenshih1/pydramsim3/releases)
[![Python](https://img.shields.io/badge/python-3.8%20%7C%203.9%20%7C%203.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-3776AB?logo=python&logoColor=white)](https://github.com/chenshih1/pydramsim3)
[![Platforms](https://img.shields.io/badge/platforms-linux%20%7C%20macos%20%7C%20windows%20%7C%20aarch64-lightgrey)](https://github.com/chenshih1/pydramsim3/actions)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Typing](https://img.shields.io/badge/typing-typed-228B22)](https://github.com/chenshih1/pydramsim3/blob/master/src/pydramsim3/py.typed)

**PyDRAMsim3** is a high-performance Python binding for
[DRAMsim3](https://github.com/umd-memsys/DRAMsim3), the cycle-accurate DRAM
simulator.  It brings gem5-style memory-system semantics to Python: a
flow-controlled `MemoryController` (submit / backpressure / retry /
outstanding tracking) on top of a C++ `SimEngine` hot loop with bulk event
export, per-transaction request tags, and zero-copy numpy trace driving.
Designed for hardware architecture research — drop a timing-accurate DRAM
model into CPU, GPU, or custom accelerator simulators, and get per-request
latency, energy, and bandwidth statistics out of the box.

## Installation

PyDRAMsim3 is not distributed as prebuilt wheels; build it from source
(requires Python >= 3.8 and a C++17 compiler; pybind11 >= 2.11 and CMake
are resolved automatically by the build system):

```bash
git clone --recursive https://github.com/chenshih1/pydramsim3.git
cd pydramsim3
pip install .
```

Source distributions are attached to the
[GitHub Releases](https://github.com/chenshih1/pydramsim3/releases) page:

```bash
pip install pydramsim3-0.1.0.tar.gz
```

Release builds enable LTO (whole-program optimization) and link the
vendored DRAMsim3 statically into a single extension module, so no
separate runtime library is bundled.

## Quick Start

```python
import pydramsim3

# MemoryController is the public entry point (gem5-aligned flow control)
tracker = pydramsim3.LatencyTracker()
mc = pydramsim3.MemoryController.from_config(
    "DDR4_8Gb_x8_2400",
    read_complete=tracker.on_read,
    write_complete=tracker.on_write,
)

print(f"Clock: {mc.clock_period:.2f} ns, burst: {mc.burst_size} B")

# Replay an address trace — backpressure and drain handled internally
trace = [(0x1000 + i * 64, i % 4 == 3) for i in range(1000)]
total_cycles = mc.replay(trace)

print(f"Simulated {total_cycles} cycles")
print(f"Avg read latency: {tracker.read_stats.avg:.1f} cycles, p99: {tracker.read_stats.p99}")

# Authoritative DRAMsim3 internal stats
stats = mc.get_stats()
ch0 = stats["0"]
print(f"Total energy: {ch0['total_energy']:.2f} pJ")
```

## MemoryController (gem5-style)

`MemoryController` replicates the flow-control semantics of gem5's `src/mem/dramsim3.cc` — backpressure, per-address outstanding tracking, retry state — without gem5 framework coupling:

```python
import pydramsim3

latencies = []
mc = pydramsim3.MemoryController.from_config(
    "DDR4_8Gb_x8_2400",
    read_complete=lambda addr, lat: latencies.append(lat),
)

# gem5-style drive loop
for cycle in range(10000):
    # submit() returns False on backpressure (gem5 recvTimingReq)
    if not mc.retry_pending:
        mc.submit(addr, is_write=False)
    mc.tick()  # advances DRAMsim3 + clears retry when space frees up

print(f"Avg latency: {sum(latencies)/len(latencies):.1f} cycles")
```

Key semantics matching gem5:
- `submit()` returns `False` when `num_outstanding >= queue_size` (admission control)
- Rejected submit sets `retry_pending`; further submits blocked until `tick()` clears it
- Per-address FIFO tracking matches DRAMsim3 callbacks to the correct transaction
- Callbacks receive `(addr, latency_cycles)` (or `(addr, latency, tag)` if tag-aware); latency computed in C++ from submit cycle

## Trace Replay & Latency Tracking

The most common research workflow — replay an address trace and collect latency percentiles:

```python
import pydramsim3

tracker = pydramsim3.LatencyTracker()
mc = pydramsim3.MemoryController.from_config(
    "DDR4_8Gb_x8_2400",
    read_complete=tracker.on_read,
    write_complete=tracker.on_write,
)

# trace: iterable of (addr, is_write)
trace = [(0x1000 + i * 64, i % 4 == 3) for i in range(1000)]
total_cycles = mc.replay(trace)  # handles backpressure + drain internally

print(f"Simulated {total_cycles} cycles")
print(f"Reads:  avg={tracker.read_stats.avg:.1f}  p99={tracker.read_stats.p99}")
print(f"Writes: avg={tracker.write_stats.avg:.1f}  p99={tracker.write_stats.p99}")
print(tracker.summary())
```

**`replay(trace, gap_cycles=0)`** — drives a `(addr, is_write)` sequence with automatic backpressure handling and a final `drain()`. Accepts any iterable (list, generator, file parser). `gap_cycles` inserts idle cycles between transactions.

**`run_trace(addrs, writes, gap_cycles=0, max_drain_cycles=None)`** — the high-throughput variant for numpy users: `addrs` (uint64) and `writes` (bool) numpy arrays drive the *entire* loop in C++ (submission, backpressure waits, gap cycles, drain) with the GIL released — a single Python-to-C++ crossing per trace. Zero-copy when the arrays are C-contiguous with the right dtypes; identical semantics (and cycle counts) to `replay()`:

```python
import numpy as np

addrs = (0x1000 + np.arange(1_000_000) * 64).astype(np.uint64)
writes = (np.arange(1_000_000) % 4 == 3)
total_cycles = mc.run_trace(addrs, writes)
```

**`drain(max_cycles=10_000_000)`** — ticks until all outstanding transactions complete. Raises `RuntimeError` on timeout.

**`LatencyTracker`** — callback-compatible collector with percentile reporting:

| Property / Method | Description |
|---|---|
| `on_read` / `on_write` | Callbacks for `MemoryController` |
| `read_stats` / `write_stats` / `all_stats` | `LatencyStats` objects |
| `num_reads` / `num_writes` | Transaction counts |
| `reset()` | Clear collected data |
| `summary()` | One-line string for logging |

**`LatencyStats`** — computed from collected latencies:

| Property | Description |
|---|---|
| `count`, `avg`, `min`, `max` | Basic stats |
| `p50`, `p90`, `p95`, `p99` | Percentiles |
| `percentile(pct)` | Arbitrary percentile (0.0–1.0) |
| `values` | Sorted list of all latencies |

## Config Discovery

DRAMsim3 ships with 80+ configs (DDR3, DDR4, HBM, GDDR5/6, LPDDR, HMC). They are bundled with the package:

```python
# List all available config names
pydramsim3.list_configs()
# ['DDR3_1Gb_x8_1333', 'DDR4_8Gb_x8_2400', 'HBM2_8Gb_x128', ...]

# Get the configs directory path
pydramsim3.configs_dir()

# Create from config name
mc = pydramsim3.MemoryController.from_config("HBM2_8Gb_x128")

# Or use a custom config file
mc = pydramsim3.MemoryController("/path/to/custom.ini")
```

## Callbacks

Completion callbacks are optional and bound at construction (matching gem5's
`DRAMsim3` SimObject, which registers callbacks once in its constructor):

```python
# No callbacks — just drive timing
mc = pydramsim3.MemoryController.from_config("DDR4_8Gb_x8_2400")

# With callbacks for integration — each receives (addr, latency_cycles)
reads_done = []
mc = pydramsim3.MemoryController.from_config(
    "DDR4_8Gb_x8_2400",
    read_complete=lambda addr, lat: reads_done.append((addr, lat)),
)
```

Callbacks fire inside `tick()` and are the integration hook for outer simulators — they tell your accelerator model "this data is now available", along with the per-transaction latency. If a callback accepts a third positional argument it is called as `(addr, latency, tag)`, where `tag` is the request id passed to `submit()` (the gem5 `PacketPtr` analog — use it to tell which request completed when several share an address). Legacy two-argument callbacks `(addr, latency)` keep working unchanged. For aggregate latency *statistics*, use `LatencyTracker` or `get_stats()` (DRAMsim3's authoritative internal data).

## Stats Collection

```python
# ... run simulation ...

# get_stats() flushes and parses DRAMsim3's JSON output
stats = mc.get_stats()
ch0 = stats["0"]

# Key metrics
avg_read_lat = ch0["average_read_latency"]
total_energy = ch0["total_energy"]          # pJ
avg_power    = ch0["average_power"]          # mW
avg_bw       = ch0["average_bandwidth"]

# Per-request latency histograms
read_hist  = ch0["read_latency"]   # {latency_cycles: count}
write_hist = ch0["write_latency"]

# File paths (for custom parsing)
mc.stats_json_path  # -> working_dir/dramsim3.json
mc.stats_txt_path   # -> working_dir/dramsim3.txt
```

## API Reference

### Module Functions

| Function | Description |
|---|---|
| `configs_dir() -> Path` | Path to bundled DRAMsim3 config files |
| `list_configs() -> list[str]` | Available config names |

### Internal binding

`pydramsim3._dramsim3.SimEngine` is the C++ hot loop behind `MemoryController`:
submission (`try_enqueue`), batched ticking (`tick(n)`), backpressure waits
(`tick_until_capacity`), bulk trace driving (`run_trace` with zero-copy numpy
arrays), outstanding tracking, and per-transaction latency all live in C++.
Completion events are exported in bulk — as Python lists
(`take_read_events`/`take_write_events`) or numpy arrays (`*_np` variants) —
instead of per-event Python callbacks.  `tick(n)`/`drain()`/
`tick_until_capacity()`/`run_trace()` release the GIL while DRAMsim3 runs;
every time-advancing method returns the number of cycles advanced, and
`current_cycle` exposes the absolute simulation clock.  Not part of the public API —
use `MemoryController` unless you need raw engine control.

### `MemoryController`

gem5-aligned controller with flow control, outstanding tracking, and per-transaction latency.

**Constructors:**

```python
MemoryController(config_file, working_dir=None, *, read_complete=None, write_complete=None, burst_size=None)
MemoryController.from_config(config_name, working_dir=None, *, read_complete=None, write_complete=None, burst_size=None)
```

**Callbacks:** `read_complete(addr: int, latency_cycles: int)` / `write_complete(addr: int, latency_cycles: int)`

**Properties:**

| Property | Type | Description |
|---|---|---|
| `num_outstanding` | `int` | Total outstanding (reads + writes) |
| `num_outstanding_reads` | `int` | Outstanding reads |
| `num_outstanding_writes` | `int` | Outstanding writes |
| `retry_pending` | `bool` | True if backpressure active (gem5 `retryReq`) |
| `current_cycle` | `int` | Current simulation cycle |
| `clock_period` | `float` | Clock period in ns |
| `queue_size` | `int` | Transaction queue depth |
| `burst_size` | `int` | Burst size in bytes |
| `stats_json_path` | `Path` | Path to JSON stats file |
| `stats_txt_path` | `Path` | Path to TXT stats file |

**Methods:**

| Method | Description |
|---|---|
| `submit(addr, is_write, tag=None) -> bool` | Submit transaction (optional request tag); False = backpressure (gem5 `recvTimingReq`) |
| `tick()` | Advance one cycle; clears retry when space available (gem5 `tick`) |
| `run(cycles) -> int` | Advance N cycles |
| `drain(max_cycles=10_000_000) -> int` | Tick until all outstanding complete; returns cycles used |
| `replay(trace, gap_cycles=0) -> int` | Drive a `(addr, is_write)` sequence with backpressure + drain |
| `run_trace(addrs, writes, gap_cycles=0, drain=True) -> int` | Numpy bulk driver; whole loop in C++, GIL released, zero-copy |
| `print_stats()` | Flush stats to output files |
| `get_stats() -> dict` | Parse and return JSON stats |
| `reset_stats()` | Reset accumulated statistics |

**Context manager:** `MemoryController` supports `with` statements.

## Performance

The simulation hot loop lives in C++ (`SimEngine`): submission, batched
ticking, backpressure waits, outstanding tracking, and per-transaction
latency all run natively, with completion events exported in bulk and the
GIL released during long runs.  `run_trace` drives whole traces with a
single zero-copy numpy crossing.

Measured on a DDR4-2400 config (`benchmarks/benchmark.py`, 100k mixed
transactions, single thread):

| Path | Throughput |
|---|---|
| `replay()` (Python loop) | ~150 ktx/s |
| `run_trace()` (numpy, zero-copy) | ~177 ktx/s |
| `run_trace()` + latency callbacks | ~175 ktx/s |

## Testing

```bash
pip install ".[test]"
pytest tests/
ruff check src/ tests/ examples/ benchmarks/
```

## Examples

See [examples/accelerator_sim.py](examples/accelerator_sim.py) for a complete example simulating a matrix-multiply accelerator's memory traffic with latency tracking.

## License

PyDRAMsim3 is licensed under the MIT License. DRAMsim3 is used under its original license (BSD-3-Clause).
