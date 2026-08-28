"""Discrete-event DRAM host: submit, wait for completions, pull results.

DRAMsim3 stays cycle-accurate in C++; Python only wakes on completions
or a host deadline.

:class:`Memory` has two time APIs:

* **Event loop** (``wait`` / ``advance_to`` / ``advance_until`` /
  ``drain``) returns :class:`Completion` lists.
* **Lockstep** (``tick`` / ``advance_by`` / ``advance_until_completion``
  / ``advance_until_in_flight_below`` / ``advance_until_accept``)
  returns cycle counts; call :meth:`Memory.pull` for events.
"""

from __future__ import annotations

import contextlib
import enum
from collections.abc import Iterable, Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, NamedTuple, NoReturn

import numpy as np
import numpy.typing as npt

from ._dramsim3 import SimEngine

__all__ = ["Completion", "DebugState", "Memory", "RequestType"]

_MAX_CYCLES = 10_000_000
_NO_DEADLINE = (1 << 64) - 1


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


class DebugState(NamedTuple):
    """Stall snapshot (no ticking).

    Field access (``st.in_flight``) and dict indexing (``st["in_flight"]``)
    both work.  ``_asdict()`` is the full mapping; iterating the tuple
    still yields values in field order.
    """

    cycle: int
    in_flight: int
    frontend: int
    outstanding_reads: int
    outstanding_writes: int
    unmatched_callbacks: int
    frontend_blocked_writes: int

    def __getitem__(self, key: str | int) -> int:  # type: ignore[override]
        if isinstance(key, str):
            try:
                return int(getattr(self, key))
            except AttributeError as exc:
                raise KeyError(key) from exc
        return int(tuple.__getitem__(self, key))


def _completion_list(engine: SimEngine) -> list[Completion]:
    """Drain engine completions in DRAMsim3 callback order."""
    addrs, lats, tags, cycles, writes = engine.take_completions()
    return [
        Completion(addr, lat, tag, bool(is_wr), cycle)
        for addr, lat, tag, cycle, is_wr in zip(addrs, lats, tags, cycles, writes)
    ]


class Memory:
    """DRAM timing model for a discrete-event host.

    Parameters
    ----------
    config_file:
        Path to a DRAMsim3 ``.ini`` config.
    working_dir:
        Directory for DRAMsim3 output files.  If omitted, a temporary
        directory is created and removed when :meth:`close` runs (also
        from ``with`` / garbage collection).
    frontend_queue:
        If True (default), ``submit`` parks on a software queue when the
        controller will not accept this address/direction.  Latency
        includes that queue wait.  Queued requests still drain with
        per-channel HOL bypass.  If False, ``submit`` returns ``None``
        when DRAMsim3 rejects this call; later calls are independent.
    outstanding_cap:
        Finite in-flight window (DRAMsim3 outstanding + frontend), in
        bursts.  ``None`` / ``0`` is unbounded.  ``"hw"`` is
        ``channels * trans_queue_size + channels`` (one extra skid slot
        per channel).  When the window is full, ``submit`` /
        ``submit_range`` refuse more traffic; the host must tick until a
        completion frees a credit.  Does not change HOL bypass of
        requests already queued.
    burst_size:
        If given, assert that it matches DRAMsim3's configured burst size.
    """

    def __init__(
        self,
        config_file: str,
        working_dir: str | None = None,
        *,
        frontend_queue: bool = True,
        outstanding_cap: int | str | None = None,
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
        self.outstanding_cap = outstanding_cap
        self._stats_cache: dict[str, Any] | None = None

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
        outstanding_cap: int | str | None = None,
        burst_size: int | None = None,
    ) -> Memory:
        """Create from a bundled config name (e.g. ``DDR4_8Gb_x8_2400``)."""
        from . import resolve_config

        return cls(
            str(resolve_config(config_name)),
            working_dir,
            frontend_queue=frontend_queue,
            outstanding_cap=outstanding_cap,
            burst_size=burst_size,
        )

    @staticmethod
    def hardware_outstanding_cap(num_channels: int, queue_size: int) -> int:
        """MC depth for one direction plus one skid slot per channel.

        HBM1 (8 channels, ``trans_queue_size=32``) is 264 bursts.
        """
        return int(num_channels) * int(queue_size) + int(num_channels)

    # -- issue --------------------------------------------------------------

    def submit(
        self,
        addr: int,
        is_write: bool | RequestType = False,
        tag: int | None = None,
    ) -> int | None:
        """Issue one burst.  Returns the request tag, or None if rejected.

        With the default frontend queue the call returns a tag unless
        ``outstanding_cap`` is full (then ``None``).  Without the
        frontend, returns None when DRAMsim3 will not accept this
        address and direction (per-channel read/write queues); later
        calls are independent.

        Check the result with ``is not None``: tag ``0`` is valid and
        would look false in a bare ``if submit(...)``.
        """
        auto_tag = tag is None
        req_tag = self._next_tag if tag is None else tag
        is_wr = bool(is_write)
        if self._frontend_queue:
            if not self._engine.park(addr, is_wr, req_tag):
                return None
            if auto_tag:
                self._next_tag += 1
            return req_tag
        if self._engine.try_admit(addr, is_wr, req_tag):
            if auto_tag:
                self._next_tag += 1
            return req_tag
        return None

    def submit_range(
        self,
        addr: int,
        count: int,
        stride: int,
        is_write: bool | RequestType = False,
        tag: int | None = None,
    ) -> int:
        """Issue up to *count* bursts at ``addr, addr+stride, ...``.

        Returns how many bursts were parked (``0`` if none).  With the
        frontend queue this parks then drains once (same admission
        order as *count* :meth:`submit` calls with no clock tick in
        between).  With ``outstanding_cap`` only bursts that fit in the
        window are parked.  Without a frontend, submits one-by-one and
        stops at the first rejection (already-accepted bursts stay in
        the controller).
        """
        if count < 0:
            raise ValueError("count must be >= 0")
        if count == 0:
            return 0
        auto_tag = tag is None
        req_tag = self._next_tag if tag is None else tag
        is_wr = bool(is_write)
        if self._frontend_queue:
            parked = int(
                self._engine.park_range(int(addr), int(count), int(stride), is_wr, int(req_tag))
            )
        else:
            parked = 0
            a = int(addr)
            step = int(stride)
            for _ in range(int(count)):
                if not self._engine.try_admit(a, is_wr, req_tag):
                    break
                a += step
                parked += 1
        if parked and auto_tag:
            self._next_tag += 1
        return parked

    def set_tag_quota(self, tag: int, remaining: int) -> None:
        """Remaining bursts for a logical request.  ``remaining=0`` clears it.

        Lockstep :meth:`advance_by` / event-loop :meth:`advance_until`
        with ``stop_on_tag_done=True`` return when any quota hits zero.
        """
        self._engine.set_tag_quota(int(tag), int(remaining))

    # -- event loop (returns Completion lists) ------------------------------

    def pull(self) -> list[Completion]:
        """Return and clear completions already collected (no ticking)."""
        return _completion_list(self._engine)

    def wait(self, max_cycles: int = _MAX_CYCLES) -> list[Completion]:
        """Tick until the next completion (or idle / *max_cycles*).

        Existing unconsumed completions are returned first without ticking.
        Raises ``RuntimeError`` if still busy after *max_cycles* with no
        new completion.  For a cycle count without pulling, use
        :meth:`advance_until_completion`.
        """
        pending = self.pull()
        if pending:
            return pending
        if not self.busy:
            return []
        self.advance_until_completion(max_cycles)
        evs = self.pull()
        if not evs and self.busy:
            self._timeout("wait", max_cycles, "no completion")
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

    def advance_until(
        self,
        target_cycle: int | None = None,
        *,
        stop_on_tag_done: bool = True,
        max_cycles: int = _MAX_CYCLES,
    ) -> list[Completion]:
        """Tick until *target_cycle*, or until a tag quota hits zero.

        ``target_cycle=None`` means no deadline: stop on tag-done, idle,
        or *max_cycles* (``0`` disables the cap).  Finite deadlines are
        not limited by *max_cycles*.  Completions already queued are
        returned first without ticking.  Raises ``RuntimeError`` if an
        unbounded wait hits the cap with traffic still in flight.
        """
        pending = self.pull()
        if pending:
            return pending
        target = _NO_DEADLINE if target_cycle is None else int(target_cycle)
        self._engine.advance_until(target, stop_on_tag_done, int(max_cycles))
        evs = self.pull()
        if target_cycle is None and not evs and self.busy:
            self._timeout("advance_until", max_cycles, "no completion")
        return evs

    def drain(self, max_cycles: int = _MAX_CYCLES) -> list[Completion]:
        """Tick until callback-tracked in-flight traffic is gone.

        Posted writes may still occupy DRAMsim3 write buffers after this
        returns; ``in_flight`` counts completions, not those buffers.
        Raises ``RuntimeError`` if still busy after *max_cycles*.
        """
        self._engine.drain(max_cycles)
        evs = self.pull()
        if self.in_flight != 0:
            self._timeout("drain", max_cycles, "still busy")
        return evs

    def completions(self) -> Iterator[Completion]:
        """Yield completions, advancing until idle (DES pull loop)."""
        while self.busy:
            evs = self.wait()
            if not evs:
                break
            yield from evs

    # -- lockstep (returns cycle counts; pull() for events) -----------------

    def tick(self, cycles: int = 1) -> int:
        """Advance *cycles* DRAM clocks.  Completions stay until :meth:`pull`."""
        return int(self._engine.tick(int(cycles)))

    def advance_by(self, cycles: int, *, stop_on_tag_done: bool = True) -> int:
        """Tick up to *cycles* from now.  Completions stay until :meth:`pull`.

        With *stop_on_tag_done*, return when a :meth:`set_tag_quota`
        counter hits zero.  Idle refresh is included; a finite jump does
        not stop early just because ``in_flight`` dropped.
        """
        return int(self._engine.advance_by(int(cycles), stop_on_tag_done))

    def advance_until_completion(self, max_cycles: int = _MAX_CYCLES) -> int:
        """Tick until the next completion, idle, or *max_cycles*.

        Completions stay until :meth:`pull`.  Unlike :meth:`wait`, this
        does not raise if still busy after *max_cycles*.
        """
        return int(self._engine.advance_until_completion(int(max_cycles)))

    def advance_until_in_flight_below(
        self,
        cap: int,
        max_cycles: int = _MAX_CYCLES,
    ) -> int:
        """Tick until ``in_flight`` is below *cap*, idle, or *max_cycles*.

        Completions stay until :meth:`pull`.  Used by DES hosts waiting
        for outstanding credit.
        """
        return int(self._engine.advance_until_in_flight_below(int(cap), int(max_cycles)))

    def advance_until_accept(
        self,
        addr: int,
        is_write: bool | RequestType = False,
        max_cycles: int = _MAX_CYCLES,
    ) -> int:
        """Tick until this address and direction would be accepted.

        Completions stay until :meth:`pull`.
        """
        return int(self._engine.advance_until_accept(int(addr), bool(is_write), int(max_cycles)))

    # -- occupancy ----------------------------------------------------------
    # busy:            in_flight > 0 (not "DRAM idle")
    # in_flight:       DRAMsim3 outstanding + frontend
    # num_outstanding*: DRAMsim3 only (excludes frontend)
    # outstanding_cap: host credit window; 0 = unbounded
    # posted writes:   callback one cycle after accept; write buffer may remain

    @property
    def busy(self) -> bool:
        """True if a burst is still awaiting a completion callback.

        This is not "the DRAM controller is idle": DRAMsim3 posts write
        completions one cycle after accept, while the write buffer may
        still drain.  Use :meth:`will_accept` if you need queue occupancy.
        """
        return self._engine.in_flight > 0

    @property
    def in_flight(self) -> int:
        """DRAMsim3 outstanding plus frontend queue depth."""
        return int(self._engine.in_flight)

    @property
    def num_outstanding(self) -> int:
        """In-flight in DRAMsim3 (excludes the frontend queue)."""
        return self._engine.num_outstanding

    @property
    def num_outstanding_reads(self) -> int:
        return self._engine.num_outstanding_reads

    @property
    def num_outstanding_writes(self) -> int:
        return self._engine.num_outstanding_writes

    @property
    def frontend_size(self) -> int:
        return self._engine.frontend_size

    @property
    def outstanding_cap(self) -> int:
        """Finite in-flight window in bursts; 0 means unbounded."""
        return int(self._engine.outstanding_cap)

    @outstanding_cap.setter
    def outstanding_cap(self, cap: int | str | None) -> None:
        if cap is None or cap == 0 or cap == "none":
            value = 0
        elif cap == "hw":
            value = self.hardware_outstanding_cap(self.num_channels, self.queue_size)
        else:
            value = int(cap)
            if value < 0:
                raise ValueError("outstanding_cap must be >= 0")
        self._engine.outstanding_cap = value

    @property
    def unmatched_callbacks(self) -> int:
        """Completion callbacks whose addr was not in the outstanding map."""
        return int(self._engine.unmatched_callbacks)

    @property
    def frontend_blocked_writes(self) -> int:
        """Parked writes waiting on an in-flight read to the same address."""
        return int(self._engine.frontend_blocked_writes)

    @property
    def debug_state(self) -> DebugState:
        """Snapshot for stall diagnosis (no ticking).

        ``in_flight`` counts callback-tracked bursts plus the frontend.
        Posted writes may still sit in DRAMsim3's write buffer after
        ``in_flight`` drops; ``will_accept(addr, True)`` is False when
        that per-channel buffer is full.
        """
        return DebugState(
            cycle=int(self.current_cycle),
            in_flight=int(self.in_flight),
            frontend=int(self.frontend_size),
            outstanding_reads=int(self.num_outstanding_reads),
            outstanding_writes=int(self.num_outstanding_writes),
            unmatched_callbacks=int(self.unmatched_callbacks),
            frontend_blocked_writes=int(self.frontend_blocked_writes),
        )

    # -- clock / geometry ---------------------------------------------------

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
    def num_channels(self) -> int:
        return int(self._engine.num_channels)

    @property
    def memory_size(self) -> int:
        """Mapped address space in bytes (``channels * channel_size``)."""
        return int(self._engine.memory_size)

    def channel_of(self, addr: int) -> int:
        """DRAMsim3 channel index for *addr* (same map as the controller)."""
        return int(self._engine.channel_of(int(addr)))

    def will_accept(self, addr: int, is_write: bool | RequestType) -> bool:
        """Whether DRAMsim3 would accept this address and direction now."""
        return bool(self._engine.will_accept(int(addr), bool(is_write)))

    # -- closed traces ------------------------------------------------------

    def replay(
        self,
        trace: Iterable[tuple],
        *,
        gap_cycles: int = 0,
        max_cycles: int = _MAX_CYCLES,
    ) -> int:
        """Submit a trace and drain.  Returns elapsed cycles.

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
                self.advance_until_accept(addr, is_write, remaining)
            if gap_cycles:
                self.tick(gap_cycles)
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
        max_drain = _MAX_CYCLES if max_drain_cycles is None else max_drain_cycles
        elapsed = self._engine.run_trace(
            addrs,
            writes,
            gap_cycles=gap_cycles,
            max_drain_cycles=max_drain,
        )
        if max_drain != 0 and self.busy:
            raise RuntimeError(f"run_trace: still busy after drain ({self._stall_detail()})")
        return elapsed

    # -- DRAMsim3 stats -----------------------------------------------------

    @property
    def stats_json_path(self) -> Path:
        return self._working_dir / "dramsim3.json"

    @property
    def stats_txt_path(self) -> Path:
        return self._working_dir / "dramsim3.txt"

    def print_stats(self) -> None:
        """Flush DRAMsim3 statistics to output files."""
        self._engine.print_stats()

    def get_stats(self, *, refresh: bool = True) -> dict[str, Any]:
        """Return DRAMsim3 JSON statistics as a dict.

        Each ``refresh=True`` call asks DRAMsim3 to rewrite the JSON
        (and can accumulate extra idle-cycle background energy).
        ``refresh=False`` returns the last snapshot, flushing once if
        nothing has been read yet.
        """
        import json

        if not refresh and self._stats_cache is not None:
            return self._stats_cache
        self._engine.print_stats()
        path = self.stats_json_path
        if not path.exists():
            raise FileNotFoundError(
                f"DRAMsim3 stats file not found at {path}. "
                f"Is working_dir ({self._working_dir}) writable?"
            )
        self._stats_cache = json.loads(path.read_text())
        return self._stats_cache

    def reset_stats(self) -> None:
        """Reset all accumulated statistics."""
        self._engine.reset_stats()

    def _stall_detail(self) -> str:
        st = self.debug_state
        return (
            f"cycle={st.cycle} in_flight={st.in_flight} "
            f"frontend={st.frontend} rd={st.outstanding_reads} "
            f"wr={st.outstanding_writes} unmatched={st.unmatched_callbacks} "
            f"blocked_wr={st.frontend_blocked_writes}"
        )

    def _timeout(self, op: str, max_cycles: int, detail: str) -> NoReturn:
        raise RuntimeError(f"{op}: {detail} after {max_cycles} cycles ({self._stall_detail()})")
