"""PyDRAMsim3 — Python bindings for the DRAMsim3 cycle-accurate memory simulator.

The public entry point is :class:`MemoryController`, a gem5-aligned DRAM
controller with flow control, outstanding tracking, and per-transaction
latency.  The C++ engine (:mod:`pydramsim3._dramsim3.SimEngine`) is an
internal implementation detail: the hot loop lives there, with bulk event
export and numpy trace driving for maximum throughput.
"""

from __future__ import annotations

from pathlib import Path

from .controller import Completion, MemoryController, RequestType
from .tracker import LatencyStats, LatencyTracker

__all__ = [
    "Completion",
    "LatencyStats",
    "LatencyTracker",
    "MemoryController",
    "RequestType",
    "configs_dir",
    "list_configs",
]

__version__ = "0.2.0"


# ---------------------------------------------------------------------------
# Config discovery
# ---------------------------------------------------------------------------


def configs_dir() -> Path:
    """Return the path to the bundled DRAMsim3 config directory.

    These are the ``.ini`` files shipped with DRAMsim3 (DDR3/4, HBM, GDDR, etc.).
    """
    return Path(__file__).parent / "configs"


def list_configs() -> list[str]:
    """List available config file stems (e.g. ``'DDR4_8Gb_x8_2400'``).

    Use these names with :meth:`MemoryController.from_config`.
    """
    cfg = configs_dir()
    if not cfg.is_dir():
        return []
    return sorted(p.stem for p in cfg.glob("*.ini"))
