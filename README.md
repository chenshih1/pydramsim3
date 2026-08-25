# PyDRAMsim3

[![CI](https://img.shields.io/github/actions/workflow/status/chenshih1/pydramsim3/ci.yml?branch=master&label=CI&logo=github)](https://github.com/chenshih1/pydramsim3/actions)
[![Release](https://img.shields.io/github/v/release/chenshih1/pydramsim3?label=release&logo=github)](https://github.com/chenshih1/pydramsim3/releases)
[![Python](https://img.shields.io/badge/python-3.8%20%7C%203.9%20%7C%203.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-3776AB?logo=python&logoColor=white)](https://github.com/chenshih1/pydramsim3)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Typing](https://img.shields.io/badge/typing-typed-228B22)](https://github.com/chenshih1/pydramsim3/blob/master/src/pydramsim3/py.typed)

**PyDRAMsim3** is a high-performance Python binding for
[DRAMsim3](https://github.com/umd-memsys/DRAMsim3), the cycle-accurate DRAM
simulator.  The public host is discrete-event: `submit`, then `wait` /
`advance_to` / `drain`.  Python wakes on completions; DRAMsim3 still runs
cycle-accurately in C++.

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

The vendored DRAMsim3 (a pinned commit) is normally fetched by
`--recursive`; if it is missing, the build downloads the pinned source
tarball into `third_party/` automatically — no manual `git submodule
update --init` needed.  To fetch from a different mirror, configure the
build with `-DPYDRAMSIM3_DRAMSIM3_TARBALL_URL=...`; disable the
auto-fetch with `-DPYDRAMSIM3_FETCH_DRAMSIM3=OFF`.

Source distributions are attached to the
[GitHub Releases](https://github.com/chenshih1/pydramsim3/releases) page:

```bash
pip install pydramsim3-0.4.0.tar.gz
```

Release builds enable LTO (whole-program optimization) and link the
vendored DRAMsim3 statically into a single extension module, so no
separate runtime library is bundled.

Platforms: buildable from source on any platform with a C++17 compiler
and CMake; CI is verified on Linux.

## Quick Start

```python
import pydramsim3

mem = pydramsim3.Memory.from_config("DDR4_8Gb_x8_2400")
mem.submit(0x1000, is_write=False)
for ev in mem.completions():          # ticks in C++ until each completion
    print(ev.cycle, ev.latency, ev.tag)

tracker = pydramsim3.LatencyTracker()
trace = [(0x1000 + i * 64, i % 4 == 3) for i in range(1000)]
for addr, is_write in trace:
    mem.submit(addr, is_write)
tracker.add(mem.drain())

print(f"Simulated {mem.current_cycle} cycles")
print(f"Avg read latency: {tracker.read_stats.avg:.1f} cycles, p99: {tracker.read_stats.p99}")

stats = mem.get_stats()
ch0 = stats["0"]
print(f"Total energy: {ch0['total_energy']:.2f} pJ")
```

`wait()` ticks in C++ until the next completion.  It raises if still busy
after `max_cycles` with no completion.  `advance_to(t, stop_on_completion=True)`
ticks until a host deadline, but stops at the first completion so follow-up
requests can issue at that timestamp.

`queue_size` is DRAMsim3's per-channel `trans_queue_size`, not a global
outstanding cap: each channel has separate read and write queues of that
depth.  Default `frontend_queue=True` parks overflow in C++ with no size
limit — the host should `wait` / `drain` so the queue does not grow without
bound.  With `frontend_queue=False`, `submit` returns `None` when that
address and direction are not accepted; a later call to a free queue can
still succeed.

If `working_dir` is omitted, DRAMsim3 stats go to a temporary directory
that is deleted when `Memory.close()` runs (`with` / GC).  Pass an explicit
path to keep `dramsim3.json`.

## Trace Replay & Latency Tracking

`replay(trace, gap_cycles=0)` enqueues a `(addr, is_write)` sequence and
`drain()`s.  For numpy traces, `run_trace` drives the whole loop in C++
(GIL released, zero-copy when the arrays are C-contiguous):

```python
import numpy as np

addrs = (0x1000 + np.arange(1_000_000) * 64).astype(np.uint64)
writes = (np.arange(1_000_000) % 4 == 3)
total_cycles = mem.run_trace(addrs, writes)
tracker.add(mem.pull())
```

`run_trace` does not use the frontend queue: each address waits until the
controller will accept it (same as `replay` with `frontend_queue=False`).

**`LatencyTracker.add(completions)`** records latencies from `drain()` /
`wait()` / `pull()`.

| Property / Method | Description |
|---|---|
| `add(completions)` | Record a batch of `Completion` events |
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
pydramsim3.list_configs()
# ['DDR3_1Gb_x8_1333', 'DDR4_8Gb_x8_2400', 'HBM2_8Gb_x128', ...]

pydramsim3.configs_dir()

mem = pydramsim3.Memory.from_config("HBM2_8Gb_x128")
mem = pydramsim3.Memory("/path/to/custom.ini")
```

## Stats Collection

```python
stats = mem.get_stats()
ch0 = stats["0"]

avg_read_lat = ch0["average_read_latency"]
total_energy = ch0["total_energy"]          # pJ
avg_power    = ch0["average_power"]          # mW
avg_bw       = ch0["average_bandwidth"]

read_hist  = ch0["read_latency"]   # {latency_cycles: count}
write_hist = ch0["write_latency"]

mem.stats_json_path  # -> working_dir/dramsim3.json
mem.stats_txt_path   # -> working_dir/dramsim3.txt
```

## API Reference

### Module Functions

| Function | Description |
|---|---|
| `configs_dir() -> Path` | Path to bundled DRAMsim3 config files |
| `list_configs() -> list[str]` | Available config names |
| `resolve_config(name) -> Path` | Path to a bundled `.ini` (stem or filename) |

### Internal binding

`pydramsim3._dramsim3.SimEngine` is the C++ hot loop behind `Memory`:
submission (`try_enqueue` / `enqueue`), batched ticking,
`tick_until_completion` / `tick_until_capacity` / `advance_to`, bulk trace
driving (`run_trace`), outstanding tracking, and per-transaction latency.
Completions are exported in callback order via `take_completions`.  Not
part of the public API — use `Memory` unless you need raw engine control.

### `Memory`

**Constructors:**

```python
Memory(config_file, working_dir=None, *, frontend_queue=True, burst_size=None)
Memory.from_config(config_name, working_dir=None, *, frontend_queue=True, burst_size=None)
```

**Methods:**

| Method | Description |
|---|---|
| `submit(addr, is_write=False, tag=None) -> int \| None` | Issue a burst; returns a tag (or `None` if `frontend_queue=False` and DRAMsim3 rejects this address/direction) |
| `wait(max_cycles=10_000_000) -> list[Completion]` | Tick in C++ until the next completion; raises if still busy with no completion |
| `advance_to(target_cycle, stop_on_completion=True)` | Tick to a host deadline; stop at the first completion by default |
| `pull() -> list[Completion]` | Return already-collected completions (no ticking) |
| `drain(max_cycles=10_000_000) -> list[Completion]` | Tick until controller and frontend are idle |
| `completions()` | Generator: advance until idle, yield each `Completion` |
| `replay(trace, gap_cycles=0, max_cycles=10_000_000) -> int` | Enqueue a trace and drain; raises if still backpressured after `max_cycles` |
| `run_trace(addrs, writes, gap_cycles=0, max_drain_cycles=None) -> int` | Numpy bulk driver; whole loop in C++ |
| `get_stats() -> dict` | Parse DRAMsim3 JSON stats |
| `close()` | Release the engine; delete a default temporary `working_dir` |

**Properties:** `busy`, `current_cycle`, `clock_period`, `queue_size`, `burst_size`, `frontend_size`, `num_outstanding`, `num_outstanding_reads`, `num_outstanding_writes`, `stats`.

Supports `with`.

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
| `run_trace()` + `LatencyTracker` | ~175 ktx/s |

## Testing

```bash
pip install ".[test]"
pytest tests/
ruff check src/ tests/ examples/ benchmarks/
```

## Examples

See [examples/accelerator_sim.py](examples/accelerator_sim.py) for a complete example simulating a matrix-multiply accelerator's memory traffic with latency tracking.

## License

PyDRAMsim3 is licensed under the MIT License. DRAMsim3 is used under its original license (MIT).
