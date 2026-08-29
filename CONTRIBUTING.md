# Contributing

Thanks for your interest in PyDRAMsim3.  The project is small and
research-oriented.

The public host is `Memory` (`src/pydramsim3/memory.py`): `submit`, then
either an event loop (`wait` / `drain`, returns completions) or lockstep
(`tick` / `advance_until_completion`, returns cycles; `pull()` for
events).  The C++ hot loop is `src/pydramsim3/sim_engine.{hpp,cpp}`;
pybind11 is `src/pydramsim3/_dramsim3.cpp`.  Python names are snake_case,
C++ names camelCase, one-to-one:

| Role | C++ | Python (`SimEngine`) | `Memory` |
|---|---|---|---|
| Admit one burst | `tryAdmit` | `try_admit` | `submit` (`frontend_queue=False`) |
| Park one burst | `park` | `park` | `submit` (default) |
| Park a burst stream | `parkRange` | `park_range` | `submit_range` (returns parked count) |
| Event-loop wrap | `advanceTo` / `advanceUntil` / `drain` | same snake_case (return cycles) | `wait` / `advance_to` / `advance_until` / `drain` (return completions) |
| Lockstep tick | `tick` / `advanceBy` / `advanceUntilCompletion` / `advanceUntilInFlightBelow` / `advanceUntilAccept` | same snake_case | same names (return cycles; `pull` separately) |
| Occupancy | `inFlight` / … | properties (`in_flight`, …) | same names |

Zero-arg engine queries are properties.  Methods that take arguments stay
methods (`will_accept`, `channel_of`).

The last cycle-driven `MemoryController` tree is branch `0.3.0`.

## Development setup

Requires a C++17 compiler, CMake (>= 3.15), and Python >= 3.8.

```bash
git clone --recursive https://github.com/chenshih1/pydramsim3.git
# or: git clone --recursive https://gitee.com/chenshih1/pydramsim3.git
cd pydramsim3
python -m venv .venv
.venv/bin/pip install -e ".[test]"   # builds the C++ extension in place
```

`--recursive` fetches the vendored DRAMsim3 sources (also used by the sdist).

## Working on the C++ layer

Rebuild with `pip install -e .` (scikit-build-core).

## Checks

```bash
.venv/bin/ruff check .            # lint
.venv/bin/ruff format --check .   # formatting
.venv/bin/mypy src/pydramsim3/    # types (CI also runs this)
.venv/bin/python -m pytest        # tests
```

A pre-commit config is provided; install it once with
`pip install pre-commit && pre-commit install`.

## Branch protection

`master` is protected on GitHub: changes must go through pull requests and
all CI checks must pass before merging.  Direct pushes to `master` are
blocked; use a feature branch and open a PR.

## Benchmarks

`benchmarks/benchmark.py` measures transaction throughput for the replay
and numpy `run_trace` paths.  Run it before and after performance changes:

```bash
.venv/bin/python benchmarks/benchmark.py
```

## Commit and PR conventions

- Keep changes focused; run the full test suite and `ruff` before pushing.
- Changelog: add an entry under `[Unreleased]` in `CHANGELOG.md`.
- Commit messages follow the existing style (`feat:`, `fix:`, `perf:`,
  `build:`, `style:`, `docs:`, `refactor:`, `test:`, `chore:`).
