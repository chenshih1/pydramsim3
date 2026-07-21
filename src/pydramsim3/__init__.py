# -*- coding: utf-8 -*-
"""PyDRAMsim3 — Python bindings for the DRAMsim3 cycle-accurate memory simulator."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Optional

from ._dramsim3 import MemorySystem as _MemorySystem

__all__ = [
    "MemorySystem",
    "configs_dir",
    "list_configs",
]

__version__ = "0.1.0"


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

    Use these names with :meth:`MemorySystem.from_config`.
    """
    cfg = configs_dir()
    if not cfg.is_dir():
        return []
    return sorted(p.stem for p in cfg.glob("*.ini"))


# ---------------------------------------------------------------------------
# Python wrapper adding convenience methods on top of the C++ binding
# ---------------------------------------------------------------------------

class MemorySystem:
    """Cycle-accurate DRAM memory system backed by DRAMsim3.

    Parameters
    ----------
    config_file:
        Path to a DRAMsim3 ``.ini`` config file.
    working_dir:
        Directory used by DRAMsim3 for output files (stats JSON/TXT).
        Defaults to the directory containing *config_file*.
    read_callback:
        Called with the transaction address when a read completes.
        Pass ``None`` (default) to ignore read completions.
    write_callback:
        Called with the transaction address when a write completes.
        Pass ``None`` (default) to ignore write completions.
    """

    def __init__(
        self,
        config_file: str,
        working_dir: Optional[str] = None,
        read_callback: Optional[Callable[[int], None]] = None,
        write_callback: Optional[Callable[[int], None]] = None,
    ) -> None:
        if working_dir is None:
            working_dir = str(Path(config_file).parent)
        self._working_dir = Path(working_dir)
        self._config_file = Path(config_file)
        self._impl = _MemorySystem(
            str(config_file),
            working_dir,
            read_callback,
            write_callback,
        )

    # -- Factory -----------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        config_name: str,
        working_dir: Optional[str] = None,
        read_callback: Optional[Callable[[int], None]] = None,
        write_callback: Optional[Callable[[int], None]] = None,
    ) -> MemorySystem:
        """Create a :class:`MemorySystem` from a bundled config name.

        Parameters
        ----------
        config_name:
            A config stem such as ``"DDR4_8Gb_x8_2400"`` (with or without
            the ``.ini`` suffix).  Use :func:`list_configs` to see all
            available names.
        working_dir:
            Output directory for DRAMsim3 stats files.  Defaults to
            the current working directory.
        read_callback / write_callback:
            Optional completion callbacks.
        """
        name = config_name if config_name.endswith(".ini") else f"{config_name}.ini"
        config_path = configs_dir() / name
        if not config_path.exists():
            available = ", ".join(list_configs()[:10])
            raise FileNotFoundError(
                f"Config '{config_name}' not found in {configs_dir()}. "
                f"Available configs include: {available}..."
            )
        if working_dir is None:
            working_dir = "."
        return cls(str(config_path), working_dir, read_callback, write_callback)

    # -- Context manager ---------------------------------------------------

    def __enter__(self) -> MemorySystem:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def __repr__(self) -> str:
        return (
            f"MemorySystem(config={self._config_file.name!r}, "
            f"tck={self.clock_period:.2f}ns, "
            f"burst={self.burst_size}B)"
        )

    # -- Delegated properties ----------------------------------------------

    @property
    def clock_period(self) -> float:
        """Clock period in nanoseconds."""
        return self._impl.clock_period

    @property
    def queue_size(self) -> int:
        """Transaction queue size."""
        return self._impl.queue_size

    @property
    def burst_size(self) -> int:
        """Burst size in bytes (data_width * burst_length)."""
        return self._impl.burst_size

    # -- Delegated methods -------------------------------------------------

    def can_accept(self, addr: int, is_write: bool) -> bool:
        """Check whether the controller can accept a new transaction."""
        return self._impl.can_accept(addr, is_write)

    def enqueue(self, addr: int, is_write: bool) -> None:
        """Enqueue a read (*is_write=False*) or write transaction.

        .. note:: Call :meth:`can_accept` first to ensure the queue is not full.
        """
        self._impl.enqueue(addr, is_write)

    def tick(self) -> None:
        """Advance the simulation by one clock cycle."""
        self._impl.tick()

    def reset_stats(self) -> None:
        """Reset all accumulated statistics."""
        self._impl.reset_stats()

    def set_callbacks(
        self,
        read_complete: Optional[Callable[[int], None]] = None,
        write_complete: Optional[Callable[[int], None]] = None,
    ) -> None:
        """Replace the read/write completion callbacks."""
        self._impl.set_callbacks(read_complete, write_complete)

    # -- Stats -------------------------------------------------------------

    @property
    def stats_json_path(self) -> Path:
        """Path to the DRAMsim3 JSON stats file (written by ``print_stats``)."""
        return self._working_dir / "dramsim3.json"

    @property
    def stats_txt_path(self) -> Path:
        """Path to the DRAMsim3 human-readable stats file."""
        return self._working_dir / "dramsim3.txt"

    def print_stats(self) -> None:
        """Flush DRAMsim3 statistics to output files.

        DRAMsim3 writes two files into *working_dir*:

        - ``dramsim3.json`` — machine-readable per-channel stats
        - ``dramsim3.txt``  — human-readable summary

        Use :meth:`get_stats` to parse the JSON directly, or check
        :attr:`stats_json_path` / :attr:`stats_txt_path` for file locations.
        """
        self._impl.print_stats()

    def get_stats(self) -> dict[str, Any]:
        """Parse and return DRAMsim3's JSON statistics as a Python dict.

        Calls :meth:`print_stats` first to ensure the file is up to date,
        then reads and parses ``dramsim3.json``.

        Returns
        -------
        dict
            A dict keyed by channel index (``"0"``, ``"1"``, ...).  Each
            channel contains fields like ``average_read_latency``,
            ``read_latency`` (per-cycle histogram), ``total_energy``, etc.

        Raises
        ------
        FileNotFoundError
            If the stats file does not exist (e.g. *working_dir* is not
            writable).
        """
        self._impl.print_stats()
        path = self.stats_json_path
        if not path.exists():
            raise FileNotFoundError(
                f"DRAMsim3 stats file not found at {path}. "
                f"Is working_dir ({self._working_dir}) writable?"
            )
        return json.loads(path.read_text())

    # -- Convenience methods -----------------------------------------------

    def run(self, cycles: int) -> int:
        """Advance the simulation by *cycles* clock ticks.

        Returns the number of cycles executed.  For tight integration
        loops, calling :meth:`tick` directly is equally efficient.
        """
        for _ in range(cycles):
            self._impl.tick()
        return cycles
