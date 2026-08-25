# PyDRAMsim3

[![CI](https://img.shields.io/github/actions/workflow/status/chenshih1/pydramsim3/ci.yml?branch=master&label=CI&logo=github)](https://github.com/chenshih1/pydramsim3/actions)
[![Release](https://img.shields.io/github/v/release/chenshih1/pydramsim3?label=release&logo=github)](https://github.com/chenshih1/pydramsim3/releases)
[![Python](https://img.shields.io/badge/python-3.8%20%7C%203.9%20%7C%203.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-3776AB?logo=python&logoColor=white)](https://github.com/chenshih1/pydramsim3)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Typing](https://img.shields.io/badge/typing-typed-228B22)](https://github.com/chenshih1/pydramsim3/blob/master/src/pydramsim3/py.typed)

Python host for [DRAMsim3](https://github.com/umd-memsys/DRAMsim3).  The
public API is discrete-event (`submit`, then `wait` / `advance_to` /
`drain`); Python wakes on completions.  DRAMsim3 itself still runs
cycle-accurately in C++.

Use it to drop a timing-accurate DRAM model into a CPU, GPU, or
accelerator simulator and read per-request latency, energy, and
bandwidth.

The cycle-driven `MemoryController` API lives on branch
[`0.3.0`](https://github.com/chenshih1/pydramsim3/tree/0.3.0) (tag `v0.3.0`).

## Installation

Build from source (Python >= 3.8, C++17).  pybind11 and CMake are pulled
in by the build backend.  There are no prebuilt wheels.

```bash
git clone --recursive https://github.com/chenshih1/pydramsim3.git
# or: git clone --recursive https://gitee.com/chenshih1/pydramsim3.git
cd pydramsim3
pip install .
```

`--recursive` fetches the pinned DRAMsim3 submodule.  If it is missing,
the build downloads that commit into `third_party/` automatically.
Override the tarball with `-DPYDRAMSIM3_DRAMSIM3_TARBALL_URL=...`, or
disable fetch with `-DPYDRAMSIM3_FETCH_DRAMSIM3=OFF`.

Sdists are on
[GitHub Releases](https://github.com/chenshih1/pydramsim3/releases):

```bash
pip install pydramsim3-0.4.1.tar.gz
```

Release builds use LTO and link DRAMsim3 statically.  CI is Linux; any
C++17 + CMake platform should build.

## Quick start

```python
import pydramsim3

mem = pydramsim3.Memory.from_config("DDR4_8Gb_x8_2400")

mem.submit(0x1000, is_write=False, tag=1)
ev = mem.wait()[0]                  # next completion
print(ev.cycle, ev.latency, ev.tag)

mem.submit(0x2000, is_write=True)
mem.advance_to(mem.current_cycle + 50)   # host deadline; stop on completion

tracker = pydramsim3.LatencyTracker()
tracker.add(mem.drain())
print(tracker.summary())

ch0 = mem.get_stats()["0"]
print(ch0["average_bandwidth"], "GB/s,", f"{ch0['total_energy']:.0f} pJ")
```

`Memory.from_config` takes a bundled config stem (`list_configs()`).
Pass `working_dir=` to keep `dramsim3.json`; otherwise stats go to a
temporary directory removed on `close()` / `with` / GC.

## Host model

| Call | What it does |
|---|---|
| `submit(addr, is_write, tag=None)` | Issue one burst.  Returns a tag.  Check `is not None` — tag `0` is valid. |
| `wait()` | Tick in C++ until the next completion (or idle). |
| `advance_to(t)` | Tick to a host deadline; default `stop_on_completion=True`. |
| `advance_until(t, stop_on_tag_done=True)` | Tick to a deadline, or until a `set_tag_quota` counter hits zero. `t=None` means no deadline. |
| `set_tag_quota(tag, n)` | Remaining bursts for a logical request. Completions decrement it. |
| `pull()` | Return completions already collected (no ticking). |
| `drain()` | Tick until controller and frontend are idle. |
| `completions()` | `wait` until idle, yield each `Completion`. |

`wait` / `drain` raise `RuntimeError` if still busy after `max_cycles`
(default 10 million).

**Backpressure.** `queue_size` is DRAMsim3's per-channel
`trans_queue_size`, not a global cap — each channel has separate read
and write queues.  Default `frontend_queue=True`: `submit` always
succeeds and parks overflow in C++ (unbounded; `wait` / `drain` so it
does not grow forever).  `frontend_queue=False`: `submit` returns
`None` when that address and direction are not accepted; a later call
to a free queue can still succeed.

**`Completion`:** `addr`, `latency`, `tag`, `is_write`, `cycle`
(engine clock after the ClockTick that produced the event).

## Closed traces

`replay` dumps a Python `(addr, is_write[, tag])` sequence and drains.
`run_trace` does the same in C++ from numpy arrays (GIL released;
zero-copy when the arrays are C-contiguous `uint64` / `bool`):

```python
import numpy as np

addrs = (0x1000 + np.arange(1_000_000) * 64).astype(np.uint64)
writes = np.arange(1_000_000) % 4 == 3
cycles = mem.run_trace(addrs, writes)
tracker.add(mem.pull())
```

`run_trace` does not use the frontend queue (same as
`replay(..., frontend_queue=False)`): each address waits until DRAMsim3
will accept it.

## Configs and stats

Bundled `.ini` files cover DDR3/4, HBM, GDDR5/6, LPDDR, HMC:

```python
pydramsim3.list_configs()
mem = pydramsim3.Memory.from_config("HBM2_8Gb_x128")
mem = pydramsim3.Memory("/path/to/custom.ini")
```

```python
ch0 = mem.get_stats()["0"]
ch0["average_read_latency"]
ch0["total_energy"]          # pJ
ch0["average_power"]         # mW
ch0["average_bandwidth"]
ch0["read_latency"]          # {cycles: count}
mem.stats_json_path          # working_dir/dramsim3.json
```

## API

```python
Memory(config_file, working_dir=None, *, frontend_queue=True, burst_size=None)
Memory.from_config(config_name, working_dir=None, *, frontend_queue=True, burst_size=None)
```

| | Role |
|---|---|
| `submit` / `wait` / `advance_to` / `pull` / `drain` / `completions` | Event loop |
| `replay` / `run_trace` | Closed traces |
| `get_stats` / `print_stats` / `reset_stats` / `stats` | DRAMsim3 JSON |
| `close()` | Drop the engine; delete a default temp `working_dir` |

**Properties:** `busy`, `current_cycle`, `clock_period`, `queue_size`,
`burst_size`, `frontend_size`, `num_outstanding`,
`num_outstanding_reads`, `num_outstanding_writes`.

Supports `with`.  Module helpers: `configs_dir()`, `list_configs()`,
`resolve_config(name)`.

**`LatencyTracker.add(completions)`** records `Completion.latency`.
Then `read_stats` / `write_stats` / `all_stats` (`count`, `avg`, `min`,
`max`, `p50`/`p90`/`p95`/`p99`, `percentile`, `values`), plus
`num_reads`, `num_writes`, `reset()`, `summary()`.

`pydramsim3._dramsim3.SimEngine` is the C++ loop behind `Memory`.  Not
part of the public API.

## Performance

Hot path in C++: submit, batched ticks, backpressure waits, outstanding
tracking, per-transaction latency.  Completions export in bulk; the GIL
is released on long runs.

DDR4-2400, 100k mixed transactions, one thread
(`benchmarks/benchmark.py`):

| Path | Throughput |
|---|---|
| `replay()` (Python loop) | ~150 ktx/s |
| `run_trace()` (numpy) | ~177 ktx/s |
| `run_trace()` + `LatencyTracker` | ~175 ktx/s |

## Development

```bash
pip install ".[test]"
pytest tests/
ruff check src/ tests/ examples/ benchmarks/
mypy src/pydramsim3/
```

Example: [examples/accelerator_sim.py](examples/accelerator_sim.py).

## License

PyDRAMsim3 is MIT.  Vendored DRAMsim3 is also MIT.
