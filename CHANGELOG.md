# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
