# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Frontend queue is sharded by `(channel, read/write)` instead of one
  flat deque.  Draining a deep frontend is O(channels) per cycle (admit
  from each queue's head) rather than O(frontend depth) with mid-deque
  erase, so bulk `submit` + `drain` / `replay` no longer quadratic in
  parked depth.  HOL bypass across channels and across read vs write is
  unchanged.  Empty-frontend ticks skip the drain; `run_trace` waits on
  `WillAccept` before `AddTransaction` to avoid skewing DRAMsim3
  interarrival stats on rejected submits.

## [0.4.1] - 2026-08-25

### Added

- `Memory.set_tag_quota(tag, remaining)` and `Memory.advance_until(target, stop_on_tag_done=True)`:
  C++ ticks until a logical request (all bursts sharing a tag) completes,
  or until a host deadline.  DES hosts can issue chained traffic at the
  completion timestamp without a Python wakeup per burst.
  `target=None` means no deadline (stop on tag-done or idle).
  `SimEngine.set_tag_quota` / `advance_until` expose the same primitive.

## [0.4.0] - 2026-08-25

### Added

- Event-driven host API: `Memory` (`submit` / `wait` / `advance_to` /
  `pull` / `drain` / `replay` / `run_trace` / `get_stats`).  Default
  `frontend_queue=True` parks overflow in C++.
- `SimEngine.tick_until_completion`, `advance_to`, `enqueue`, `frontend_size`,
  `in_flight`, `take_completions` (callback order, mixed read/write), and
  `take_*_completions` (addr, latency, tag, complete_cycle).
- `resolve_config(name)` for bundled `.ini` lookup.
- `LatencyTracker.add(completions)` records latencies from `Completion` events.
- Default `working_dir` is a temporary directory (removed on `close()`).

### Changed

- Public host is DES-only.  `MemoryController` (`tick` / `run` / per-cycle
  callbacks) is removed; closed traces use `Memory.replay` / `Memory.run_trace`.
- Frontend queue drains with HOL bypass: a blocked head does not stall
  later requests that DRAMsim3 will accept (other channel or the other
  read/write queue).
- Backpressure follows DRAMsim3: per-channel, separate read and write
  queues of `trans_queue_size`.  There is no extra global outstanding cap.
- `Memory.drain` raises if still busy after `max_cycles`.
- `Memory.wait` raises if still busy after `max_cycles` with no completion.
- `run_trace` drain keys on in-flight (controller + frontend), matching
  `drain()`.

### Removed

- `MemoryController` cycle-driven host and sticky-retry-era flow control.
- Global outstanding cap of `queue_size`; admission follows DRAMsim3's
  per-channel read/write queues.
- 3-field `take_*_events` engine API; use `take_completions` /
  `take_*_completions`.

### Fixed

- `Memory.replay` with `frontend_queue=False` waits for controller capacity
  instead of dropping rejected submits, and raises instead of spinning if
  still backpressured after `max_cycles`.
- `run_trace` raises if a single submit stays blocked for 10 million cycles.

## [0.3.0] - 2026-08-19

The last cycle-driven `MemoryController` release.  Preserved on branch
`0.3.0` (tag `v0.3.0` is the version bump; the branch includes later
docs/CI commits on that line).

### Added

- `RequestType` enum, `completions()` generator, and `stats` property.

## [0.2.0] - 2026-08-19

### Changed

- sdist-only install path; LTO and static DRAMsim3 link.
- CI slimmed to Ubuntu, Python 3.8–3.13, sdist smoke.

## [0.1.0] - 2026-08-14

### Added

- `MemoryController`: gem5-aligned flow control (`submit`/`tick`/`run`/`drain`/`replay`)
  with backpressure, retry semantics, outstanding tracking, and per-transaction latency.
- `SimEngine`: C++ hot loop with batched ticking, backpressure waits
  (`tick_until_capacity`), bulk event export (list and numpy variants), and
  request `tag` support (gem5 `PacketPtr` analog).
- `run_trace`: zero-copy numpy trace driver; the whole submission/wait/drain
  loop runs in C++ with the GIL released.
- `LatencyTracker`/`LatencyStats` with cached percentile statistics.
- Bundled DRAMsim3 config discovery (`configs_dir`, `list_configs`) and
  `get_stats()` JSON parsing.

### Changed

- Completion events carry `(addr, latency, tag)`; callbacks are adapted by
  signature, so legacy two-argument callbacks keep working.
- All time-advancing methods return the number of cycles advanced;
  `drain` defaults are unified at 10 million cycles.

### Fixed

- Write-backpressure deadlock under sustained mixed traffic (DRAMsim3 write
  completion callbacks fire one cycle after submission; waits now key on
  DRAMsim3's own acceptance check).
- CMake >= 4 compatibility for the vendored DRAMsim3 submodule
  (`CMAKE_POLICY_VERSION_MINIMUM`).
- macOS rpath (`@loader_path`) and Windows import library
  (`WINDOWS_EXPORT_ALL_SYMBOLS`) so wheels load the bundled DRAMsim3 library.

### Infrastructure

- CI matrix (3 OS x Python 3.8-3.13) with packaging job, wheel asset checks,
  and a fresh-venv wheel smoke test.
- Ruff linting/formatting, pre-commit hooks, EditorConfig.
- PEP 639 LICENSE metadata; explicit sdist inclusion of the DRAMsim3 submodule.
