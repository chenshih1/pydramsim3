# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- `Memory.replay` no longer livelocks when `submit` fails for reasons other
  than DRAM-queue backpressure (`outstanding_cap` full, or a write that
  aliases an in-flight read with `frontend_queue=False`).
  `advance_until_accept` now waits until `try_admit` would succeed, not
  only `WillAcceptTransaction`.
- `try_admit` / no-frontend `submit` honor `outstanding_cap`, matching
  `park`.  `replay` only credit-waits when the cap is actually full, so a
  DRAM-queue or R→W reject cannot over-drain via
  `advance_until_in_flight_below`.
- `Memory.reset_stats` clears the `get_stats(refresh=False)` snapshot so
  the next read flushes the new stats epoch.

## [0.5.0] - 2026-08-26

### Added

- Lockstep time primitives on `Memory`: `tick`, `advance_by`,
  `advance_until_completion`, `advance_until_in_flight_below`,
  `advance_until_accept`.  These return cycle counts and leave
  completions for `pull()`, so a DES host does not need `SimEngine`.
  `wait` / `advance_to` / `advance_until` / `drain` still return events.
- `Memory.unmatched_callbacks` and `Memory.frontend_blocked_writes`
  properties (already in `debug_state`).
- `DebugState` named tuple for `debug_state` (still dict-indexable).

### Changed

Breaking rename (no aliases).  Stay on `v0.4.4` if you need the old names.

- Issue verbs: C++ `tryAdmit` / `park` / `parkRange` (Python `try_admit` /
  `park` / `park_range`).  `Memory.submit` is unchanged.  `submit_range`
  returns how many bursts were parked (`0` if none).  `Memory.park_range`
  is removed.
- Time verbs: `advanceUntilCompletion` / `advanceUntilAccept` (Python
  `advance_until_completion` / `advance_until_accept`).  `tick`,
  `advance_to` / `until` / `by`, `drain`, and `wait` are unchanged.
- Zero-arg occupancy queries are properties: `in_flight`, `frontend_size`,
  `num_outstanding*`, `unmatched_callbacks`, `frontend_blocked_writes`,
  `outstanding_cap`.  `Memory.debug_state` is a property.
- Types: `PendingTransaction`, `Completion`; `frontend_queues_`.
- Include guard `PYDRAMSIM3_SIM_ENGINE_HPP`.
- `Memory` source is grouped by role (issue, event loop, lockstep,
  occupancy).  `debug_state` returns `DebugState`.
- Internal: host time primitives share one `advanceLocked` stop-predicate
  loop (`has_target` rather than a UINT64_MAX sentinel).  Public names
  and stop semantics are unchanged.

### Fixed

- Ruff ignores vendored `third_party/` and Markdown, so `ruff check .`
  / `ruff format --check .` match the development docs.

### Removed

- `Memory.stats`.  Use `get_stats` / `print_stats` / `reset_stats`.

## [0.4.4] - 2026-08-26

### Added

- Stall diagnostics: `Memory.debug_state()`, `unmatched_callbacks`,
  `frontend_blocked_writes`, `will_accept(addr, is_write)`.  `wait` /
  `drain` / unbounded `advance_until` errors include this snapshot.
- Physical map: `Memory.memory_size` (bytes) and `Memory.channel_of(addr)`,
  from the same `.ini` mapping DRAMsim3 uses.  Hosts should wrap or
  allocate inside this space; tracking keys are full host addresses.
- `get_stats(refresh=False)` returns the last JSON snapshot without
  asking DRAMsim3 to rewrite the file (avoids extra idle-energy
  accumulation).  Default `refresh=True` is unchanged.

### Changed

- Unbounded `advance_until(None)` / `advanceUntil(UINT64_MAX)` stops
  after `max_cycles` (default 10 million; `0` disables the cap).
  Finite lockstep deadlines still ignore this cap.
- Frontend write drain skips only a write whose address has an
  outstanding read; later writes on that channel may still enter
  DRAMsim3 (avoids HOL-stalling the whole write queue).
- `busy` / `in_flight` are documented as callback-tracked occupancy.
  Posted writes can remain in DRAMsim3 write buffers after `in_flight`
  drops.

### Fixed

- Unmatched DRAMsim3 callbacks are counted instead of dropped silently
  (ghost `in_flight` was a DES empty-heap hang).

## [0.4.3] - 2026-08-25

### Added

- Finite outstanding window: `Memory(outstanding_cap=...)` /
  `SimEngine.set_outstanding_cap`.  `0` (default) keeps unbounded
  parking.  `"hw"` is `channels * trans_queue_size + channels` (HBM1:
  264 bursts).  `enqueue` / `enqueue_range` / `park_range` refuse more
  traffic when the window is full; the host must tick until a
  completion frees a credit.  Per-channel HOL bypass of already-queued
  requests is unchanged.
- `Memory.park_range`, `Memory.in_flight`, `Memory.num_channels`.
- `SimEngine.advance_until_in_flight_below(cap)` ticks until the
  in-flight count drops below *cap* (credit wait for a DES host).

## [0.4.2] - 2026-08-25

### Changed

- Frontend drain is per-(channel, read/write) FIFO heads admitted in
  global enqueue order.  Same HOL bypass as 0.4.1 (a blocked channel
  does not stall a free one), but each `ClockTick` is O(channels) instead
  of scanning the whole parked queue.  Channel index is taken from the
  `.ini` via `dramsim3::Config` (identical to `BaseDRAMSystem::GetChannel`);
  the vendored DRAMsim3 sources are unchanged.
- `Memory.submit_range` / `SimEngine.enqueue_range` park a burst stream
  and drain once (same admission order as repeated `submit`/`enqueue`
  with no clock tick in between).
- `SimEngine.advance_by(n)` ticks up to *n* cycles from now (used by
  tight DES hosts).  `advance_until` / `advance_by` only release the
  GIL when the jump is at least 64 ticks, so 1-tick host gaps do not
  pay a GIL round-trip per DRAM cycle.

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
