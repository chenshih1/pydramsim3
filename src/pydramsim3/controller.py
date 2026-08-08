# -*- coding: utf-8 -*-
"""gem5-aligned memory controller wrapping DRAMsim3.

Mirrors the flow-control and outstanding-tracking semantics of
gem5's ``src/mem/dramsim3.cc`` without SimObject/Port/Event coupling.
"""

from __future__ import annotations

import logging
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from ._dramsim3 import DRAMsim3Wrapper

__all__ = ["MemoryController"]

logger = logging.getLogger(__name__)


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
        read_complete: Optional[Callable[[int, int], None]] = None,
        write_complete: Optional[Callable[[int, int], None]] = None,
        burst_size: Optional[int] = None,
    ) -> None:
        if working_dir is None:
            working_dir = str(Path(config_file).parent)
        self._working_dir = Path(working_dir)
        self._config_file = Path(config_file)

        self._user_read_cb = read_complete
        self._user_write_cb = write_complete

        self._wrapper = DRAMsim3Wrapper(
            str(config_file),
            working_dir,
            self._on_read_complete,
            self._on_write_complete,
        )

        if burst_size is not None and burst_size != self._wrapper.burst_size:
            raise ValueError(
                f"burst_size {burst_size} does not match DRAMsim3 "
                f"configured burst size {self._wrapper.burst_size}"
            )

        # gem5: std::unordered_map<Addr, std::queue<PacketPtr>>
        self._outstanding_reads: dict[int, deque[int]] = {}
        self._outstanding_writes: dict[int, deque[int]] = {}

        # gem5: nbrOutstandingReads / nbrOutstandingWrites
        self._nbr_outstanding_reads: int = 0
        self._nbr_outstanding_writes: int = 0

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
        read_complete: Optional[Callable[[int, int], None]] = None,
        write_complete: Optional[Callable[[int, int], None]] = None,
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
            logger.debug("cycle %d: submit rejected (retry pending)", self._current_cycle)
            return False

        can_accept = self.num_outstanding < self._wrapper.queue_size

        if can_accept:
            if not self._wrapper.can_accept(addr, is_write):
                self._retry_req = True
                logger.debug("cycle %d: submit rejected (dramsim3 queue full)", self._current_cycle)
                return False

            if not is_write:
                self._outstanding_reads.setdefault(addr, deque()).append(
                    self._current_cycle
                )
                self._nbr_outstanding_reads += 1
            else:
                self._outstanding_writes.setdefault(addr, deque()).append(
                    self._current_cycle
                )
                self._nbr_outstanding_writes += 1

            self._wrapper.enqueue(addr, is_write)
            return True
        else:
            self._retry_req = True
            logger.debug(
                "cycle %d: submit rejected (outstanding %d >= queue_size %d)",
                self._current_cycle, self.num_outstanding, self._wrapper.queue_size,
            )
            return False

    def tick(self) -> None:
        """Advance one clock cycle.

        Corresponds to gem5 ``DRAMsim3::tick``.
        """
        self._wrapper.tick()
        self._current_cycle += 1

        if self._retry_req and self.num_outstanding < self._wrapper.queue_size:
            self._retry_req = False
            logger.debug("cycle %d: retry cleared (outstanding %d)", self._current_cycle, self.num_outstanding)

    def run(self, cycles: int) -> int:
        """Advance *cycles* ticks.  Returns the number of cycles executed."""
        for _ in range(cycles):
            self.tick()
        return cycles

    def drain(self, max_cycles: int = 100_000) -> int:
        """Tick until all outstanding transactions complete.

        Returns the number of cycles consumed.  Raises ``RuntimeError``
        if transactions are still outstanding after *max_cycles*.
        """
        for i in range(max_cycles):
            if self.num_outstanding == 0:
                if i > 0:
                    logger.info("drain completed in %d cycles (cycle %d)", i, self._current_cycle)
                return i
            self.tick()
        if self.num_outstanding != 0:
            raise RuntimeError(
                f"drain: {self.num_outstanding} transactions still "
                f"outstanding after {max_cycles} cycles"
            )
        return max_cycles

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
                self.tick()
            count += 1
            if count % 1000 == 0:
                logger.debug("replay: %d transactions submitted (cycle %d)", count, self._current_cycle)
            if gap_cycles:
                self.run(gap_cycles)
        self.drain()
        elapsed = self._current_cycle - start
        logger.info("replay completed: %d transactions in %d cycles", count, elapsed)
        return elapsed

    # -- gem5 readComplete / writeComplete ---------------------------------

    def _on_read_complete(self, addr: int) -> None:
        q = self._outstanding_reads[addr]
        submit_cycle = q.popleft()
        if not q:
            del self._outstanding_reads[addr]

        assert self._nbr_outstanding_reads != 0
        self._nbr_outstanding_reads -= 1

        latency = self._current_cycle - submit_cycle
        if self._user_read_cb is not None:
            self._user_read_cb(addr, latency)

    def _on_write_complete(self, addr: int) -> None:
        q = self._outstanding_writes[addr]
        submit_cycle = q.popleft()
        if not q:
            del self._outstanding_writes[addr]

        assert self._nbr_outstanding_writes != 0
        self._nbr_outstanding_writes -= 1

        latency = self._current_cycle - submit_cycle
        if self._user_write_cb is not None:
            self._user_write_cb(addr, latency)

    # -- Properties (mirror gem5 accessors) --------------------------------

    @property
    def num_outstanding(self) -> int:
        """Total outstanding transactions (gem5 ``nbrOutstanding()``)."""
        return self._nbr_outstanding_reads + self._nbr_outstanding_writes

    @property
    def num_outstanding_reads(self) -> int:
        return self._nbr_outstanding_reads

    @property
    def num_outstanding_writes(self) -> int:
        return self._nbr_outstanding_writes

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
        return self._wrapper.clock_period

    @property
    def queue_size(self) -> int:
        """Transaction queue depth."""
        return self._wrapper.queue_size

    @property
    def burst_size(self) -> int:
        """Burst size in bytes."""
        return self._wrapper.burst_size

    # -- Stats (delegated) -------------------------------------------------

    @property
    def stats_json_path(self) -> Path:
        return self._working_dir / "dramsim3.json"

    @property
    def stats_txt_path(self) -> Path:
        return self._working_dir / "dramsim3.txt"

    def print_stats(self) -> None:
        """Flush DRAMsim3 statistics to output files."""
        self._wrapper.print_stats()

    def get_stats(self) -> dict[str, Any]:
        """Return DRAMsim3 JSON statistics as a dict."""
        import json

        self._wrapper.print_stats()
        path = self.stats_json_path
        if not path.exists():
            raise FileNotFoundError(
                f"DRAMsim3 stats file not found at {path}. "
                f"Is working_dir ({self._working_dir}) writable?"
            )
        return json.loads(path.read_text())

    def reset_stats(self) -> None:
        """Reset all accumulated statistics."""
        self._wrapper.reset_stats()
