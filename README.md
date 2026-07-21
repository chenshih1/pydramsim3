# PyDRAMsim3

Python bindings for [DRAMsim3](https://github.com/umd-memsys/DRAMsim3), a cycle-accurate DRAM simulator. Designed for hardware architecture research — integrate DRAM timing models into CPU, GPU, or custom accelerator simulators.

## Installation

```bash
pip install -e .
```

Requires Python >= 3.8, pybind11 >= 2.6 (auto-resolved by the build system).

## Quick Start

```python
import pydramsim3

# Use a bundled DRAM config — no need to locate .ini files manually
mem = pydramsim3.MemorySystem.from_config("DDR4_8Gb_x8_2400")

print(f"Clock: {mem.clock_period:.2f} ns, burst: {mem.burst_size} B")

# Drive the simulator cycle by cycle
for cycle in range(1000):
    addr = 0x1000 + (cycle % 16) * 64
    if mem.can_accept(addr, is_write=False):
        mem.enqueue(addr, is_write=False)
    mem.tick()

# Collect structured stats
stats = mem.get_stats()
ch0 = stats["0"]
print(f"Avg read latency: {ch0['average_read_latency']:.1f} cycles")
print(f"Total energy: {ch0['total_energy']:.2f} pJ")
```

## Config Discovery

DRAMsim3 ships with 80+ configs (DDR3, DDR4, HBM, GDDR5/6, LPDDR, HMC). They are bundled with the package:

```python
# List all available config names
pydramsim3.list_configs()
# ['DDR3_1Gb_x8_1333', 'DDR4_8Gb_x8_2400', 'HBM2_8Gb_x128', ...]

# Get the configs directory path
pydramsim3.configs_dir()

# Create from config name
mem = pydramsim3.MemorySystem.from_config("HBM2_8Gb_x128")

# Or use a custom config file
mem = pydramsim3.MemorySystem("/path/to/custom.ini")
```

## Callbacks

Completion callbacks are optional — pass `None` (or omit) if you only need timing:

```python
# No callbacks — just drive timing
mem = pydramsim3.MemorySystem.from_config("DDR4_8Gb_x8_2400")

# With callbacks for integration
reads_done = []
mem = pydramsim3.MemorySystem.from_config(
    "DDR4_8Gb_x8_2400",
    read_callback=lambda addr: reads_done.append(addr),
)

# Replace callbacks later
mem.set_callbacks(read_complete=new_cb)
```

Callbacks fire inside `tick()` and are the integration hook for outer simulators — they tell your accelerator model "this data is now available". For latency *statistics*, use `get_stats()` which reads DRAMsim3's authoritative internal data.

## Stats Collection

```python
# ... run simulation ...

# get_stats() flushes and parses DRAMsim3's JSON output
stats = mem.get_stats()
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
mem.stats_json_path  # -> working_dir/dramsim3.json
mem.stats_txt_path   # -> working_dir/dramsim3.txt
```

## API Reference

### Module Functions

| Function | Description |
|---|---|
| `configs_dir() -> Path` | Path to bundled DRAMsim3 config files |
| `list_configs() -> list[str]` | Available config names |

### `MemorySystem`

**Constructors:**

```python
MemorySystem(config_file, working_dir=None, read_callback=None, write_callback=None)
MemorySystem.from_config(config_name, working_dir=None, read_callback=None, write_callback=None)
```

**Properties:**

| Property | Type | Description |
|---|---|---|
| `clock_period` | `float` | Clock period in ns |
| `queue_size` | `int` | Transaction queue depth |
| `burst_size` | `int` | Burst size in bytes |
| `stats_json_path` | `Path` | Path to JSON stats file |
| `stats_txt_path` | `Path` | Path to TXT stats file |

**Methods:**

| Method | Description |
|---|---|
| `can_accept(addr, is_write) -> bool` | Check if controller accepts a new transaction |
| `enqueue(addr, is_write)` | Enqueue a read/write transaction |
| `tick()` | Advance simulation by one clock cycle |
| `run(cycles) -> int` | Advance simulation by N cycles |
| `print_stats()` | Flush stats to output files |
| `get_stats() -> dict` | Parse and return JSON stats |
| `reset_stats()` | Reset accumulated statistics |
| `set_callbacks(read_complete, write_complete)` | Replace completion callbacks |

**Context manager:** `MemorySystem` supports `with` statements.

## Testing

```bash
pip install pytest
pytest tests/ -v
```

## Examples

See [examples/accelerator_sim.py](examples/accelerator_sim.py) for a complete example simulating a matrix-multiply accelerator's memory traffic with latency tracking.

## License

PyDRAMsim3 is licensed under the MIT License. DRAMsim3 is used under its original license (BSD-3-Clause).
