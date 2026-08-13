# -*- coding: utf-8 -*-
"""gem5-aligned memory controller wrapping DRAMsim3.

Mirrors the flow-control and outstanding-tracking semantics of
gem5's ``src/mem/dramsim3.cc`` without SimObject/Port/Event coupling.

The hot loop (submission, ticking, outstanding tracking, latency) lives in
the C++ :class:`_dramsim3.SimEngine`; this module is a thin Python shell
adding gem5-style flow control, retry semantics and optional user callbacks.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import numpy as np
import numpy.typing as npt

from ._dramsim3 import SimEngine

__all__ = ["MemoryController"]

logger = logging.getLogger(__name__)

_CALLBACK = Callable[[int, int], None]


class MemoryController:
    """Cycle-accurate DRAM controller with gem5-style flow control.

    Parameters
    ----------
    config_file:
        Path to a DRAMsim3 ``.ini`` config file.
    working_dir:
        Directory for DRAMsim3 output files.  Defaults to the config
        file's parent directory.
    read_complete:
        Called as ``read_complete(addr, latency_cycles)`` when a read
        finishes inside DRAMsim3.
    write_complete:
        Called as ``write_complete(addr, latency_cycles)`` when a write
        finishes inside DRAMsim3.
    burst_size:
        If given, assert that it matches DRAMsim3's configured burst
        size (analogous to gem5 checking ``cacheLineSize``).
    """

    def __init__(
        self,
        config_file: str,
        working_dir: Optional[str] = None,
        *,
        read_complete: Optional[_CALLBACK] = None,
        write_complete: Optional[_CALLBACK] = None,
        burst_size: Optional[int] = None,
    ) -> None:
        if working_dir is None:
            working_dir = str(Path(config_file).parent)
        self._working_dir = Path(working_dir)
        self._config_file = Path(config_file)

        # Callback backing fields (private properties below).
        self.__user_read_cb: Optional[_CALLBACK] = None
        self.__user_write_cb: Optional[_CALLBACK] = None
        self._collect: bool = (read_complete is not None) or (write_complete is not None)

        self._engine = SimEngine(
            str(config_file),
            working_dir,
            collect_events=self._collect,
        )

        # Cached invariants: avoid per-submit crossing into C++.
        self._queue_size = self._engine.queue_size
        self._burst_size = self._engine.burst_size

        # Goes through the property setters, which keep the engine's
        # event-collection flag in sync.
        self._user_read_cb = read_complete
        self._user_write_cb = write_complete

        if burst_size is not None and burst_size != self._burst_size:
            raise ValueError(
                f"burst_size {burst_size} does not match DRAMsim3 "
                f"configured burst size {self._burst_size}"
            )

        # gem5: retryReq
        self._retry_req: bool = False

        self._current_cycle: int = 0

    # -- Factory -----------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        config_name: str,
        working_dir: Optional[str] = None,
        *,
        read_complete: Optional[_CALLBACK] = None,
        write_complete: Optional[_CALLBACK] = None,
        burst_size: Optional[int] = None,
    ) -> MemoryController:
        """Create from a bundled config name (e.g. ``"DDR4_8Gb_x8_2400"``)."""
        from . import configs_dir, list_configs

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
        return cls(
            str(config_path),
            working_dir,
            read_complete=read_complete,
            write_complete=write_complete,
            burst_size=burst_size,
        )

    # -- Context manager ---------------------------------------------------

    def __enter__(self) -> MemoryController:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def __repr__(self) -> str:
        return (
            f"MemoryController(config={self._config_file.name!r}, "
            f"tck={self.clock_period:.2f}ns, "
            f"burst={self.burst_size}B, "
            f"outstanding={self.num_outstanding})"
        )

    # -- Core API (mirrors gem5 recvTimingReq / tick) ----------------------

    def submit(self, addr: int, is_write: bool) -> bool:
        """Submit a transaction.  Returns False on backpressure.

        Corresponds to gem5 ``DRAMsim3::recvTimingReq``.
        """
        if self._retry_req:
            return False

        if not self._engine.try_enqueue(addr, is_write):
            self._retry_req = True
            return False
        return True

    def tick(self, cycles: int = 1) -> int:
        """Advance *cycles* clock cycles; returns cycles advanced.

        Corresponds to gem5 ``DRAMsim3::tick``.
        """
        if cycles <= 0:
            return 0
        self._engine.tick(cycles)
        self._current_cycle += cycles
        if self._collect:
            self._dispatch_completions()

        if self._retry_req and self.num_outstanding < self._queue_size:
            self._retry_req = False
        return cycles

    def run(self, cycles: int) -> int:
        """Advance *cycles* ticks.  Returns the number of cycles executed."""
        return self.tick(cycles)

    def drain(self, max_cycles: int = 10_000_000) -> int:
        """Tick until all outstanding transactions complete.

        Returns the number of cycles consumed.  Raises ``RuntimeError``
        if transactions are still outstanding after *max_cycles*.
        """
        if self.num_outstanding == 0:
            return 0
        cycles = self._engine.drain(max_cycles)
        self._current_cycle += cycles
        if self._collect:
            self._dispatch_completions()
        if self.num_outstanding != 0:
            raise RuntimeError(
                f"drain: {self.num_outstanding} transactions still "
                f"outstanding after {max_cycles} cycles"
            )
        if self._retry_req:
            self._retry_req = False
        if cycles > 0:
            logger.info("drain completed in %d cycles (cycle %d)", cycles, self._current_cycle)
        return cycles

    def replay(
        self,
        trace: Iterable[tuple[int, bool]],
        *,
        gap_cycles: int = 0,
    ) -> int:
        """Drive a sequence of ``(addr, is_write)`` transactions.

        Handles backpressure internally: when ``submit`` is rejected,
        ticks until retry clears before retrying the same transaction.

        Parameters
        ----------
        trace:
            Iterable of ``(addr, is_write)`` pairs.
        gap_cycles:
            Idle cycles to insert between consecutive transactions
            (models inter-request spacing).

        Returns
        -------
        int
            Total cycles elapsed (including drain at the end).
        """
        start = self._current_cycle
        count = 0
        for addr, is_write in trace:
            while not self.submit(addr, is_write):
                # Backpressure: wait for capacity inside C++ (single crossing
                # instead of a Python submit/tick ping-pong per cycle).  The
                # precise per-transaction acceptance check is done in C++.
                n = self._engine.tick_until_capacity(addr, is_write)
                self._current_cycle += n
                if self._collect:
                    self._dispatch_completions()
                self._retry_req = False
            count += 1
            if count % 1000 == 0:
                logger.debug("replay: %d transactions submitted (cycle %d)", count, self._current_cycle)
            if gap_cycles:
                self.run(gap_cycles)
        self.drain()
        elapsed = self._current_cycle - start
        logger.info("replay completed: %d transactions in %d cycles", count, elapsed)
        return elapsed

    def run_trace(
        self,
        addrs: npt.NDArray[np.uint64],
        writes: npt.NDArray[np.bool_],
        *,
        gap_cycles: int = 0,
        drain: bool = True,
    ) -> int:
        """Drive a trace stored in numpy arrays, entirely inside C++.

        Parameters
        ----------
        addrs:
            Numpy array of uint64 addresses (C-contiguous for zero-copy).
        writes:
            Numpy array of bools, same length as ``addrs``.
        gap_cycles:
            Idle cycles inserted after each transaction.
        drain:
            If True (default), tick until all outstanding transactions
            complete before returning.

        Returns
        -------
        int
            Total cycles elapsed (including drain).

        The submission loop, backpressure waits, gap cycles and drain all
        run in C++ with the GIL released — one Python-to-C++ crossing for
        the whole trace.  Semantics match :meth:`replay`.
        """
        n = np.asarray(addrs).size
        start = self._current_cycle
        elapsed = self._engine.run_trace(
            addrs,
            writes,
            gap_cycles=gap_cycles,
            max_drain_cycles=10_000_000 if drain else 0,
        )
        self._current_cycle += elapsed
        if self._collect:
            self._dispatch_completions()
        self._retry_req = False

        if drain and self.num_outstanding != 0:
            raise RuntimeError(
                f"run_trace: {self.num_outstanding} transactions still "
                f"outstanding after drain"
            )
        logger.info("run_trace completed: %d transactions in %d cycles", n, elapsed)
        return elapsed

    # -- Completion dispatch -----------------------------------------------

    def _dispatch_completions(self) -> None:
        """Drain engine completion events into the user callbacks, if any."""
        if self._user_read_cb is not None:
            addrs, lats = self._engine.take_read_events()
            for addr, lat in zip(addrs, lats):
                self._user_read_cb(addr, lat)
        if self._user_write_cb is not None:
            addrs, lats = self._engine.take_write_events()
            for addr, lat in zip(addrs, lats):
                self._user_write_cb(addr, lat)

    # -- Callbacks (hot-swappable) -----------------------------------------
    #
    # Private properties: assigning a callback enables the engine's C++
    # event collection for that direction, so late-registered callbacks
    # still observe completions.

    @property
    def _user_read_cb(self) -> Optional[_CALLBACK]:
        return self.__user_read_cb

    @_user_read_cb.setter
    def _user_read_cb(self, cb: Optional[_CALLBACK]) -> None:
        self.__user_read_cb = cb
        self._collect = cb is not None or self.__user_write_cb is not None
        self._engine.set_collect(self._collect)

    @property
    def _user_write_cb(self) -> Optional[_CALLBACK]:
        return self.__user_write_cb

    @_user_write_cb.setter
    def _user_write_cb(self, cb: Optional[_CALLBACK]) -> None:
        self.__user_write_cb = cb
        self._collect = cb is not None or self.__user_read_cb is not None
        self._engine.set_collect(self._collect)

    def set_callbacks(
        self,
        read_complete: Optional[_CALLBACK] = None,
        write_complete: Optional[_CALLBACK] = None,
    ) -> None:
        """Replace the completion callbacks.

        Assigning a callback enables C++ event collection for that direction;
        passing ``None`` for both disables it.
        """
        self._user_read_cb = read_complete
        self._user_write_cb = write_complete

    # -- Properties (mirror gem5 accessors) --------------------------------

    @property
    def num_outstanding(self) -> int:
        """Total outstanding transactions (gem5 ``nbrOutstanding()``)."""
        return self._engine.num_outstanding()

    @property
    def num_outstanding_reads(self) -> int:
        return self._engine.num_outstanding_reads()

    @property
    def num_outstanding_writes(self) -> int:
        return self._engine.num_outstanding_writes()

    @property
    def retry_pending(self) -> bool:
        """True if a previous submit was rejected and retry not yet cleared.

        Mirrors gem5 ``retryReq``: once tick() detects capacity, this
        clears automatically — analogous to gem5 calling sendRetryReq().
        """
        return self._retry_req

    @property
    def current_cycle(self) -> int:
        return self._current_cycle

    @property
    def clock_period(self) -> float:
        """Clock period in nanoseconds."""
        return self._engine.clock_period

    @property
    def queue_size(self) -> int:
        """Transaction queue depth."""
        return self._queue_size

    @property
    def burst_size(self) -> int:
        """Burst size in bytes."""
        return self._burst_size

    # -- Stats (delegated) -------------------------------------------------

    @property
    def stats_json_path(self) -> Path:
        return self._working_dir / "dramsim3.json"

    @property
    def stats_txt_path(self) -> Path:
        return self._working_dir / "dramsim3.txt"

    def print_stats(self) -> None:
        """Flush DRAMsim3 statistics to output files."""
        self._engine.print_stats()

    def get_stats(self) -> dict[str, Any]:
        """Return DRAMsim3 JSON statistics as a dict."""
        import json

        self._engine.print_stats()
        path = self.stats_json_path
        if not path.exists():
            raise FileNotFoundError(
                f"DRAMsim3 stats file not found at {path}. "
                f"Is working_dir ({self._working_dir}) writable?"
            )
        return json.loads(path.read_text())

    def reset_stats(self) -> None:
        """Reset all accumulated statistics."""
        self._engine.reset_stats()
