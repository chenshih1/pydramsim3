"""Event-driven DRAM server: submit, wait for completions, pull results.

The C++ engine still runs DRAMsim3 cycle-accurately; Python only wakes on
completions or a host deadline.
"""

from __future__ import annotations

import contextlib
import enum
from collections.abc import Iterable, Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, NamedTuple

import numpy as np
import numpy.typing as npt

from ._dramsim3 import SimEngine

__all__ = ["Completion", "Memory", "RequestType"]


class RequestType(enum.Enum):
    """Direction of a memory transaction."""

    READ = 0
    WRITE = 1

    def __bool__(self) -> bool:
        """True for writes, so a RequestType drops into bool ``is_write`` args."""
        return self is RequestType.WRITE


class Completion(NamedTuple):
    """A completed memory transaction.

    ``cycle`` is the engine clock after the ClockTick that produced the
    completion.
    """

    addr: int
    latency: int
    tag: int
    is_write: bool
    cycle: int = 0


def _completion_list(engine: SimEngine) -> list[Completion]:
    """Drain engine completions in DRAMsim3 callback order."""
    addrs, lats, tags, cycles, writes = engine.take_completions()
    return [
        Completion(addr, lat, tag, bool(is_wr), cycle)
        for addr, lat, tag, cycle, is_wr in zip(addrs, lats, tags, cycles, writes)
    ]


class Memory:
    """Timed memory server for a discrete-event host.

    Parameters
    ----------
    config_file:
        Path to a DRAMsim3 ``.ini`` config.
    working_dir:
        Directory for DRAMsim3 output files.  If omitted, a temporary
        directory is created and removed when :meth:`close` runs (also
        from ``with`` / garbage collection).
    frontend_queue:
        If True (default), ``submit`` always succeeds and parks on a
        software queue when the controller will not accept this
        address/direction.  Latency includes that queue wait.  The
        queue is unbounded: the host should ``wait`` / ``drain`` so it
        does not grow without bound.  If False, ``submit`` returns
        ``None`` when DRAMsim3 rejects this call; later calls are
        independent.
    burst_size:
        If given, assert that it matches DRAMsim3's configured burst size.
    """

    def __init__(
        self,
        config_file: str,
        working_dir: str | None = None,
        *,
        frontend_queue: bool = True,
        burst_size: int | None = None,
    ) -> None:
        self._tmp: TemporaryDirectory | None = None
        if working_dir is None:
            self._tmp = TemporaryDirectory(prefix="pydramsim3_")
            working_dir = self._tmp.name
        self._working_dir = Path(working_dir)
        self._config_file = Path(config_file)
        self._engine = SimEngine(str(config_file), working_dir, True)
        self._frontend_queue = bool(frontend_queue)
        self._next_tag = 1
        if burst_size is not None and burst_size != self._engine.burst_size:
            raise ValueError(
                f"burst_size {burst_size} does not match DRAMsim3 "
                f"configured burst size {self._engine.burst_size}"
            )

    def close(self) -> None:
        """Release the engine, then delete a default temporary working_dir."""
        engine = getattr(self, "_engine", None)
        if engine is not None:
            del self._engine
        tmp = getattr(self, "_tmp", None)
        if tmp is not None:
            tmp.cleanup()
            self._tmp = None

    def __enter__(self) -> Memory:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.close()

    def __repr__(self) -> str:
        return (
            f"Memory(config={self._config_file.name!r}, "
            f"tck={self.clock_period:.2f}ns, "
            f"burst={self.burst_size}B, "
            f"busy={self.busy})"
        )

    @classmethod
    def from_config(
        cls,
        config_name: str,
        working_dir: str | None = None,
        *,
        frontend_queue: bool = True,
        burst_size: int | None = None,
    ) -> Memory:
        """Create from a bundled config name (e.g. ``\"DDR4_8Gb_x8_2400\"``)."""
        from . import resolve_config

        return cls(
            str(resolve_config(config_name)),
            working_dir,
            frontend_queue=frontend_queue,
            burst_size=burst_size,
        )

    def submit(
        self,
        addr: int,
        is_write: bool | RequestType = False,
        tag: int | None = None,
    ) -> int | None:
        """Issue one burst.  Returns the request tag, or None if rejected.

        With the default frontend queue the call always returns a tag.
        Without it, returns None when DRAMsim3 will not accept this
        address and direction (per-channel read/write queues); later
        calls are independent.

        Check the result with ``is not None``: tag ``0`` is valid and
        would look false in a bare ``if submit(...)``.
        """
        auto_tag = tag is None
        req_tag = self._next_tag if tag is None else tag
        is_wr = bool(is_write)
        if self._frontend_queue:
            self._engine.enqueue(addr, is_wr, req_tag)
            if auto_tag:
                self._next_tag += 1
            return req_tag
        if self._engine.try_enqueue(addr, is_wr, req_tag):
            if auto_tag:
                self._next_tag += 1
            return req_tag
        return None

    def pull(self) -> list[Completion]:
        """Return and clear completions already collected (no ticking)."""
        return _completion_list(self._engine)

    def wait(self, max_cycles: int = 10_000_000) -> list[Completion]:
        """Tick in C++ until the next completion (or idle / max_cycles).

        Existing unconsumed completions are returned first without ticking.
        Raises ``RuntimeError`` if still busy after *max_cycles* with no
        new completion.
        """
        pending = self.pull()
        if pending:
            return pending
        if not self.busy:
            return []
        self._engine.tick_until_completion(max_cycles)
        evs = self.pull()
        if not evs and self.busy:
            raise RuntimeError(
                f"wait: no completion after {max_cycles} cycles "
                f"(outstanding={self.num_outstanding}, frontend={self.frontend_size})"
            )
        return evs

    def advance_to(
        self,
        target_cycle: int,
        *,
        stop_on_completion: bool = True,
    ) -> list[Completion]:
        """Tick until ``current_cycle`` reaches *target_cycle*.

        If *stop_on_completion* is true (default), stop at the ClockTick
        that produces the first new completion so the host can issue
        follow-up requests at that timestamp.
        """
        pending = self.pull()
        if pending and stop_on_completion:
            return pending
        self._engine.advance_to(int(target_cycle), stop_on_completion)
        pending.extend(self.pull())
        return pending

    def drain(self, max_cycles: int = 10_000_000) -> list[Completion]:
        """Tick until nothing is in flight (controller + frontend queue).

        Raises ``RuntimeError`` if still busy after *max_cycles*.
        """
        self._engine.drain(max_cycles)
        evs = self.pull()
        if self._engine.in_flight() != 0:
            raise RuntimeError(
                f"drain: still busy after {max_cycles} cycles "
                f"(outstanding={self.num_outstanding}, frontend={self.frontend_size})"
            )
        return evs

    @property
    def busy(self) -> bool:
        """True if the controller or frontend still holds a transaction."""
        return self._engine.in_flight() > 0

    @property
    def frontend_size(self) -> int:
        return self._engine.frontend_size()

    @property
    def current_cycle(self) -> int:
        return self._engine.current_cycle

    @property
    def clock_period(self) -> float:
        """Clock period in nanoseconds."""
        return self._engine.clock_period

    @property
    def queue_size(self) -> int:
        """Per-channel DRAMsim3 transaction queue depth (``trans_queue_size``).

        Not a global outstanding cap: each channel has separate read and
        write queues of this depth.
        """
        return self._engine.queue_size

    @property
    def burst_size(self) -> int:
        """Burst size in bytes."""
        return self._engine.burst_size

    @property
    def num_outstanding(self) -> int:
        """In-flight in DRAMsim3 (excludes the frontend queue)."""
        return self._engine.num_outstanding()

    @property
    def num_outstanding_reads(self) -> int:
        return self._engine.num_outstanding_reads()

    @property
    def num_outstanding_writes(self) -> int:
        return self._engine.num_outstanding_writes()

    def completions(self) -> Iterator[Completion]:
        """Yield completions, advancing until idle (DES pull loop)."""
        while self.busy:
            evs = self.wait()
            if not evs:
                break
            yield from evs

    def replay(
        self,
        trace: Iterable[tuple],
        *,
        gap_cycles: int = 0,
        max_cycles: int = 10_000_000,
    ) -> int:
        """Enqueue a trace and drain.  Returns elapsed cycles.

        With ``frontend_queue=False``, waits until DRAMsim3 will accept
        this address and direction instead of dropping rejected submits.
        ``gap_cycles`` inserts idle DRAM clocks after each accepted submit.
        Raises ``RuntimeError`` if still backpressured or busy after
        *max_cycles*.
        """
        start = self.current_cycle
        for entry in trace:
            addr = entry[0]
            is_write = bool(entry[1])
            tag = entry[2] if len(entry) > 2 else None
            while self.submit(addr, is_write, tag) is None:
                remaining = max_cycles - (self.current_cycle - start)
                if remaining <= 0:
                    raise RuntimeError(f"replay: still backpressured after {max_cycles} cycles")
                self._engine.tick_until_capacity(addr, bool(is_write), remaining)
            if gap_cycles:
                self._engine.tick(gap_cycles)
        remaining = max_cycles - (self.current_cycle - start)
        self.drain(max_cycles=max(remaining, 1))
        return self.current_cycle - start

    def run_trace(
        self,
        addrs: npt.NDArray[np.uint64],
        writes: npt.NDArray[np.bool_],
        *,
        gap_cycles: int = 0,
        max_drain_cycles: int | None = None,
    ) -> int:
        """Drive a numpy trace entirely in C++ (GIL released).

        Semantics match :meth:`replay` without the frontend queue: each
        address is submitted when the controller will accept it.
        Completions stay in the engine until :meth:`pull` or :meth:`drain`.
        """
        max_drain = 10_000_000 if max_drain_cycles is None else max_drain_cycles
        elapsed = self._engine.run_trace(
            addrs,
            writes,
            gap_cycles=gap_cycles,
            max_drain_cycles=max_drain,
        )
        if max_drain != 0 and self.busy:
            raise RuntimeError(
                f"run_trace: still busy after drain "
                f"(outstanding={self.num_outstanding}, frontend={self.frontend_size})"
            )
        return elapsed

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

    @property
    def stats(self) -> dict[str, Any]:
        """DRAMsim3 JSON statistics as a dict (alias for :meth:`get_stats`)."""
        return self.get_stats()

    def reset_stats(self) -> None:
        """Reset all accumulated statistics."""
        self._engine.reset_stats()
