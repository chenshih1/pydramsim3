# Contributing

Thanks for your interest in PyDRAMsim3! This project is small and
research-oriented; any help is welcome.

## Development setup

Requires a C++17 compiler, CMake (>= 3.15), and Python >= 3.8.

```bash
git clone --recursive https://github.com/chenshih1/pydramsim3.git
cd pydramsim3
python -m venv .venv
.venv/bin/pip install -e ".[test]"   # builds the C++ extension in place
```

The `--recursive` flag is required: the DRAMsim3 submodule carries the
vendored simulator sources (also used by the sdist).

## Working on the C++ layer

The extension is rebuilt by `pip install -e .` (scikit-build-core).  The
hot loop lives in `src/pydramsim3/sim_engine.{hpp,cpp}`; the pybind11
surface is `src/pydramsim3/_dramsim3.cpp`.  Keep the Python-facing names
snake_case and the C++ names camelCase, one-to-one.

## Checks

```bash
.venv/bin/ruff check .            # lint
.venv/bin/ruff format --check .   # formatting
.venv/bin/python -m pytest        # tests
```

A pre-commit config is provided; install it once with
`pip install pre-commit && pre-commit install`.

A pre-push hook runs lint + tests before every push:

```bash
scripts/pre-push.sh install   # once, installs .git/hooks/pre-push
git push --no-verify          # emergency bypass
```

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
