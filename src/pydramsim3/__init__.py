"""PyDRAMsim3 — event-driven Python host for DRAMsim3.

:class:`Memory` is the public API: ``submit``, then ``wait`` /
``advance_to`` / ``drain``.  Python wakes on completions; DRAMsim3 runs
cycle-accurately in C++.
"""

from __future__ import annotations

from pathlib import Path

from .memory import Completion, Memory, RequestType
from .tracker import LatencyStats, LatencyTracker

__all__ = [
    "Completion",
    "LatencyStats",
    "LatencyTracker",
    "Memory",
    "RequestType",
    "configs_dir",
    "list_configs",
    "resolve_config",
]

__version__ = "0.4.1"


def configs_dir() -> Path:
    """Return the path to the bundled DRAMsim3 config directory.

    These are the ``.ini`` files shipped with DRAMsim3 (DDR3/4, HBM, GDDR, etc.).
    """
    return Path(__file__).parent / "configs"


def list_configs() -> list[str]:
    """List available config file stems (e.g. ``'DDR4_8Gb_x8_2400'``).

    Use these names with :meth:`Memory.from_config`.
    """
    cfg = configs_dir()
    if not cfg.is_dir():
        return []
    return sorted(p.stem for p in cfg.glob("*.ini"))


def resolve_config(config_name: str) -> Path:
    """Return the path to a bundled DRAMsim3 ``.ini`` (stem or filename)."""
    name = config_name if config_name.endswith(".ini") else f"{config_name}.ini"
    config_path = configs_dir() / name
    if not config_path.exists():
        available = ", ".join(list_configs()[:10])
        raise FileNotFoundError(
            f"Config '{config_name}' not found in {configs_dir()}. "
            f"Available configs include: {available}..."
        )
    return config_path
