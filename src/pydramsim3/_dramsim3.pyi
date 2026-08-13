from typing import Tuple

import numpy as np
import numpy.typing as npt


class SimEngine:
    """High-performance C++ hot loop over DRAMsim3.

    The hot loop (submission, backpressure waits, batching, outstanding
    tracking, per-transaction latency) lives entirely in C++.  Completion
    events are exported in bulk as ``(addr, latency)`` pairs via
    ``take_read_events`` / ``take_write_events`` (Python lists) or their
    ``_np`` variants (numpy arrays).  ``tick`` / ``drain`` /
    ``tick_until_capacity`` / ``run_trace`` release the GIL while running.
    """

    def __init__(
        self,
        config_file: str,
        working_dir: str,
        collect_events: bool = True,
    ) -> None: ...

    def try_enqueue(self, addr: int, is_write: bool, tag: int = 0) -> bool: ...

    def tick(self, cycles: int = 1) -> int: ...
    def drain(self, max_cycles: int = 10000000) -> int: ...
    def tick_until_capacity(
        self, addr: int, is_write: bool, max_cycles: int = 10000000
    ) -> int: ...
    def run_trace(
        self,
        addrs: npt.NDArray[np.uint64],
        writes: npt.NDArray[np.bool_],
        gap_cycles: int = 0,
        max_drain_cycles: int = 10000000,
    ) -> int: ...

    def set_collect(self, collect: bool) -> None: ...
    def take_read_events(self) -> Tuple[list[int], list[int], list[int]]: ...
    def take_write_events(self) -> Tuple[list[int], list[int], list[int]]: ...
    def take_read_events_np(
        self,
    ) -> Tuple[
        npt.NDArray[np.uint64], npt.NDArray[np.uint64], npt.NDArray[np.uint64]
    ]: ...
    def take_write_events_np(
        self,
    ) -> Tuple[
        npt.NDArray[np.uint64], npt.NDArray[np.uint64], npt.NDArray[np.uint64]
    ]: ...

    def num_outstanding(self) -> int: ...
    def num_outstanding_reads(self) -> int: ...
    def num_outstanding_writes(self) -> int: ...

    def print_stats(self) -> None: ...
    def reset_stats(self) -> None: ...

    @property
    def cycle(self) -> int:
        """Absolute simulation cycle (engine clock)."""
        ...

    @property
    def clock_period(self) -> float: ...
    @property
    def queue_size(self) -> int: ...
    @property
    def burst_size(self) -> int: ...
