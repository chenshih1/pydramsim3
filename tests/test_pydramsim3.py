import numpy as np
import pytest

from pydramsim3 import (
    Completion,
    LatencyStats,
    LatencyTracker,
    Memory,
    RequestType,
    configs_dir,
    list_configs,
    resolve_config,
)
from pydramsim3._dramsim3 import SimEngine

# ---------------------------------------------------------------------------
# Config discovery
# ---------------------------------------------------------------------------


class TestConfigDiscovery:
    def test_configs_dir_exists(self):
        d = configs_dir()
        assert d.is_dir()

    def test_list_configs_nonempty(self):
        cfgs = list_configs()
        assert len(cfgs) > 50

    def test_list_configs_contains_known(self):
        cfgs = list_configs()
        assert "DDR4_8Gb_x8_2400" in cfgs
        assert "HBM2_8Gb_x128" in cfgs
        assert "DDR3_4Gb_x8_1600" in cfgs

    def test_list_configs_sorted(self):
        cfgs = list_configs()
        assert cfgs == sorted(cfgs)

    def test_resolve_config(self):
        p = resolve_config("DDR4_8Gb_x8_2400")
        assert p.name == "DDR4_8Gb_x8_2400.ini"
        assert p.exists()
        assert resolve_config("DDR4_8Gb_x8_2400.ini") == p

    def test_resolve_config_missing(self):
        with pytest.raises(FileNotFoundError):
            resolve_config("NOT_A_REAL_CONFIG")


# ---------------------------------------------------------------------------
# SimEngine — high-performance C++ hot loop
# ---------------------------------------------------------------------------


class TestSimEngine:
    """Tests the C++ SimEngine (bulk events, batching, backpressure waits)."""

    @staticmethod
    def _make(tmp_path, collect=True):
        cfg = str(configs_dir() / "DDR4_8Gb_x8_2400.ini")
        return SimEngine(cfg, str(tmp_path), collect)

    def test_construct(self, tmp_path):
        e = self._make(tmp_path)
        assert e.clock_period > 0
        assert e.queue_size == 32
        assert e.burst_size == 64
        assert e.current_cycle == 0

    def test_tick_returns_cycles_and_advances_clock(self, tmp_path):
        e = self._make(tmp_path)
        assert e.tick(100) == 100
        assert e.current_cycle == 100
        e.tick()
        assert e.current_cycle == 101

    def test_drain_returns_cycles_used(self, tmp_path):
        e = self._make(tmp_path)
        assert e.drain() == 0

    def test_take_read_completions_np(self, tmp_path):
        e = self._make(tmp_path)
        e.try_admit(0x1000, False)
        e.tick(500)
        addrs, lats, _tags, _cycles = e.take_read_completions_np()
        assert addrs.dtype == np.uint64
        assert addrs.tolist() == [0x1000]
        assert lats.tolist()[0] > 0
        addrs, _, _, _ = e.take_read_completions_np()
        assert len(addrs) == 0

    def test_take_write_completions_np(self, tmp_path):
        e = self._make(tmp_path)
        e.try_admit(0x2000, True)
        e.tick(10)
        addrs, _, _, _ = e.take_write_completions_np()
        assert addrs.tolist() == [0x2000]

    def test_try_admit_and_take_read_completions(self, tmp_path):
        e = self._make(tmp_path)
        assert e.try_admit(0x1000, False)
        e.tick(500)
        addrs, lats, _tags, _ = e.take_read_completions()
        assert addrs == [0x1000]
        assert len(lats) == 1 and lats[0] > 0

    def test_take_completions_clears_buffer(self, tmp_path):
        e = self._make(tmp_path)
        e.try_admit(0x1000, False)
        e.tick(500)
        addrs, _, _, _ = e.take_read_completions()
        assert len(addrs) == 1
        addrs, _, _, _ = e.take_read_completions()
        assert addrs == []

    def test_write_completions(self, tmp_path):
        e = self._make(tmp_path)
        e.try_admit(0x2000, True)
        e.tick(10)
        addrs, _lats, _tags, _ = e.take_write_completions()
        assert addrs == [0x2000]

    def test_tag_roundtrip(self, tmp_path):
        e = self._make(tmp_path)
        assert e.try_admit(0x1000, False, tag=42)
        e.tick(500)
        addrs, _lats, tags, _ = e.take_read_completions()
        assert addrs == [0x1000]
        assert tags == [42]

    def test_tag_default_zero(self, tmp_path):
        e = self._make(tmp_path)
        assert e.try_admit(0x1000, False)
        e.tick(500)
        _, _, tags, _ = e.take_read_completions()
        assert tags == [0]

    def test_tags_fifo_same_address(self, tmp_path):
        e = self._make(tmp_path)
        e.try_admit(0x1000, False, tag=1)
        e.try_admit(0x1000, False, tag=2)
        e.try_admit(0x1000, False, tag=3)
        e.tick(1000)
        _, _, tags, _ = e.take_read_completions()
        # FIFO per address: completions arrive in submission order
        assert tags == [1, 2, 3]

    def test_backpressure_at_queue_size(self, tmp_path):
        e = self._make(tmp_path, collect=False)
        accepted = 0
        while e.try_admit(0x1000 + accepted * 64, False):
            accepted += 1
        assert accepted == e.queue_size
        assert not e.try_admit(0x9999, False)

    def test_advance_until_accept_waits(self, tmp_path):
        e = self._make(tmp_path, collect=False)
        accepted = 0
        while e.try_admit(0x1000 + accepted * 64, False):
            accepted += 1
        assert accepted == e.queue_size
        # queue is now full; waiting must advance cycles and free a slot
        addr = 0x1000 + accepted * 64
        n = e.advance_until_accept(addr, False)
        assert n > 0
        assert e.try_admit(addr, False)

    def test_advance_until_accept_returns_zero_when_free(self, tmp_path):
        e = self._make(tmp_path)
        assert e.advance_until_accept(0x1000, False) == 0

    def test_advance_until_accept_waits_out_raw_write(self, tmp_path):
        """WillAccept can be true while try_admit rejects a write that aliases
        an in-flight read; the wait must still advance until the read completes."""
        e = self._make(tmp_path)
        addr = 0x1000
        assert e.try_admit(addr, False)
        assert e.will_accept(addr, True)
        assert not e.try_admit(addr, True)
        n = e.advance_until_accept(addr, True)
        assert n > 0
        assert e.try_admit(addr, True)
        e.drain()

    def test_try_admit_honors_outstanding_cap(self, tmp_path):
        e = self._make(tmp_path)
        e.outstanding_cap = 2
        assert e.try_admit(0x1000, False)
        assert e.try_admit(0x1040, False)
        assert e.in_flight == 2
        assert not e.try_admit(0x1080, False)
        # Cap is not part of can-admit / advance_until_accept.
        assert e.will_accept(0x1080, False)
        assert e.advance_until_accept(0x1080, False) == 0
        e.advance_until_in_flight_below(2)
        assert e.in_flight < 2
        assert e.try_admit(0x1080, False)
        e.drain()

    def test_drain(self, tmp_path):
        e = self._make(tmp_path, collect=False)
        for i in range(16):
            assert e.try_admit(0x1000 + i * 64, False)
        assert e.num_outstanding == 16
        cycles = e.drain(1_000_000)
        assert cycles > 0
        assert e.num_outstanding == 0

    def test_drain_empty_returns_zero(self, tmp_path):
        e = self._make(tmp_path)
        assert e.drain(1000) == 0

    def test_set_collect_clears_events(self, tmp_path):
        e = self._make(tmp_path)
        e.try_admit(0x1000, False)
        e.tick(500)
        e.set_collect(False)
        addrs, _, _, _ = e.take_read_completions()
        assert addrs == []

    def test_multiple_completions_ordered(self, tmp_path):
        e = self._make(tmp_path)
        for _i in range(4):
            e.try_admit(0x1000, False)
        e.tick(1000)
        addrs, lats, _tags, _ = e.take_read_completions()
        assert len(addrs) == 4
        # FIFO per address: latencies non-decreasing
        assert lats == sorted(lats)

    def test_write_buffer_backpressure_no_deadlock(self, tmp_path):
        """Regression: DRAMsim3 write callbacks fire one cycle after submit,
        so the write_buffer_ can stay full while the outstanding counter
        reads zero; waiting must key on DRAMsim3's own acceptance check."""
        e = self._make(tmp_path, collect=False)
        for i in range(300):
            while not e.try_admit(0x1000 + i * 64, True):
                e.advance_until_accept(0x1000 + i * 64, True)
        e.drain(10_000_000)
        assert e.num_outstanding == 0

    def test_sustained_mixed_replay_no_deadlock(self, tmp_path):
        """Regression: sustained mixed traffic used to busy-spin in Python."""
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        trace = [(0x1000 + i * 64, i % 2 == 1) for i in range(500)]
        cycles = mem.replay(trace)
        assert cycles > 0
        assert mem.num_outstanding == 0


# ---------------------------------------------------------------------------
# run_trace — numpy bulk driver
# ---------------------------------------------------------------------------


class TestRunTrace:
    """Tests the numpy zero-copy trace driver (C++ hot loop)."""

    @staticmethod
    def _make_trace(n, stride=64, write_odd=True):
        addrs = (0x1000 + np.arange(n) * stride).astype(np.uint64)
        writes = np.arange(n) % 2 == 1 if write_odd else np.zeros(n, dtype=bool)
        return addrs, writes

    @staticmethod
    def _mem(tmp_path, **kw):
        return Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path), **kw)

    def test_matches_replay_cycles(self, tmp_path):
        addrs, writes = self._make_trace(256)
        c1 = self._mem(tmp_path, frontend_queue=False).replay(
            [(int(a), bool(w)) for a, w in zip(addrs, writes)]
        )
        c2 = self._mem(tmp_path).run_trace(addrs, writes)
        assert c1 == c2

    def test_cycle_counter_advances(self, tmp_path):
        mem = self._mem(tmp_path)
        addrs, writes = self._make_trace(16)
        cycles = mem.run_trace(addrs, writes)
        assert cycles > 0
        assert mem.current_cycle == cycles

    def test_pull_receives_events(self, tmp_path):
        mem = self._mem(tmp_path)
        addrs, wflags = self._make_trace(64)
        mem.run_trace(addrs, wflags)
        evs = mem.pull()
        assert sum(1 for e in evs if not e.is_write) == 32
        assert sum(1 for e in evs if e.is_write) == 32
        assert mem.num_outstanding == 0

    def test_empty_trace(self, tmp_path):
        mem = self._mem(tmp_path)
        a = np.zeros(0, dtype=np.uint64)
        w = np.zeros(0, dtype=bool)
        assert mem.run_trace(a, w) == 0

    def test_drain_false(self, tmp_path):
        mem = self._mem(tmp_path)
        addrs, writes = self._make_trace(16, write_odd=False)
        mem.run_trace(addrs, writes, max_drain_cycles=0)
        assert mem.num_outstanding > 0
        mem.drain()
        assert mem.num_outstanding == 0

    def test_length_mismatch(self, tmp_path):
        mem = self._mem(tmp_path)
        with pytest.raises(ValueError):
            mem.run_trace(np.zeros(4, dtype=np.uint64), np.zeros(3, dtype=bool))

    def test_accepts_lists(self, tmp_path):
        mem = self._mem(tmp_path)
        mem.run_trace([0x1000, 0x1040, 0x1080], [False, False, False])
        mem.drain()
        assert mem.num_outstanding == 0

    def test_strided_views(self, tmp_path):
        mem = self._mem(tmp_path)
        addrs, writes = self._make_trace(128, write_odd=False)
        cycles = mem.run_trace(addrs[::2], writes[::2])
        assert cycles > 0
        mem.drain()
        assert mem.num_outstanding == 0

    def test_gap_cycles(self, tmp_path):
        addrs, writes = self._make_trace(16)
        c0 = self._mem(tmp_path).run_trace(addrs, writes)
        c1 = self._mem(tmp_path).run_trace(addrs, writes, gap_cycles=50)
        assert c1 > c0


class TestMemoryTags:
    def test_tagged_completion(self, tmp_path):
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        assert mem.submit(0x1000, False, tag=7) == 7
        evs = mem.drain()
        assert [(e.addr, e.tag) for e in evs] == [(0x1000, 7)]

    def test_same_address_distinct_tags(self, tmp_path):
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        mem.submit(0x1000, False, tag=11)
        mem.submit(0x1000, False, tag=22)
        evs = mem.drain()
        assert [e.tag for e in evs] == [11, 22]


class TestDifferentConfigs:
    @pytest.mark.parametrize(
        "config_name",
        [
            "DDR3_4Gb_x8_1600",
            "HBM2_8Gb_x128",
            "LPDDR4_8Gb_x16_2400",
        ],
    )
    def test_config_loads(self, config_name, tmp_path):
        mem = Memory.from_config(config_name, working_dir=str(tmp_path))
        assert mem.clock_period > 0
        assert mem.burst_size > 0
        mem.advance_to(10, stop_on_completion=False)
        assert mem.current_cycle == 10

    def test_hbm_channels_are_independent(self, tmp_path):
        mem = Memory.from_config("HBM2_8Gb_x128", working_dir=str(tmp_path), frontend_queue=False)
        accepted = 0
        while mem.submit(0x1000 + accepted * 4096, False) is not None:
            accepted += 1
            if accepted > 8 * mem.queue_size + 8:
                break
        assert accepted > mem.queue_size
        mem.drain()
        assert not mem.busy


class TestMemoryConstruction:
    def test_from_config(self, tmp_path):
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        assert mem.clock_period > 0
        assert mem.queue_size == 32
        assert mem.burst_size == 64

    def test_from_config_invalid(self):
        with pytest.raises(FileNotFoundError, match="not found"):
            Memory.from_config("NONEXISTENT_CONFIG")

    def test_burst_size_validation(self, tmp_path):
        with pytest.raises(ValueError, match="does not match"):
            Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path), burst_size=128)

    def test_burst_size_valid(self, tmp_path):
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path), burst_size=64)
        assert mem.burst_size == 64

    def test_repr(self, tmp_path):
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        r = repr(mem)
        assert "DDR4_8Gb_x8_2400.ini" in r
        assert "busy=False" in r

    def test_default_working_dir_is_temporary(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with Memory.from_config("DDR4_8Gb_x8_2400") as mem:
            mem.submit(0x1000, False)
            mem.drain()
            assert "0" in mem.get_stats()
            assert mem.stats_json_path.exists()
            assert mem.stats_json_path.parent.resolve() != tmp_path.resolve()
        assert list(tmp_path.glob("dramsim3.*")) == []


class TestMemoryFlowControl:
    @pytest.fixture
    def mem(self, tmp_path):
        return Memory.from_config(
            "DDR4_8Gb_x8_2400", working_dir=str(tmp_path), frontend_queue=False
        )

    def test_submit_accepted(self, mem):
        assert mem.submit(0x1000, False) is not None
        assert mem.num_outstanding == 1

    def test_backpressure_at_queue_size(self, mem):
        accepted = 0
        while mem.submit(0x1000 + accepted * 64, False) is not None:
            accepted += 1
        assert accepted == mem.queue_size

    def test_rejected_after_full(self, mem):
        for i in range(mem.queue_size):
            mem.submit(0x1000 + i * 64, False)
        assert mem.submit(0x9999, False) is None

    def test_reject_does_not_block_later_submits(self, mem):
        for i in range(mem.queue_size):
            mem.submit(0x1000 + i * 64, False)
        assert mem.submit(0x9999, False) is None
        mem.wait()
        assert mem.submit(0xAAAA, False) is not None

    def test_read_queue_full_does_not_block_write(self, mem):
        for i in range(mem.queue_size):
            assert mem.submit(0x1000 + i * 64, False) is not None
        assert mem.submit(0x9000, False) is None
        assert mem.submit(0xA000, True) is not None
        assert mem.num_outstanding == mem.queue_size + 1

    def test_write_queue_full_does_not_block_read(self, mem):
        for i in range(mem.queue_size):
            assert mem.submit(0x1000 + i * 64, True) is not None
        assert mem.submit(0x9000, True) is None
        assert mem.submit(0xA000, False) is not None
        assert mem.num_outstanding_reads == 1
        assert mem.num_outstanding_writes == mem.queue_size


class TestMemoryOutstanding:
    @pytest.fixture
    def mem(self, tmp_path):
        return Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))

    def test_read_outstanding_tracking(self, mem):
        mem.submit(0x1000, False)
        mem.submit(0x2000, False)
        assert mem.num_outstanding_reads == 2
        assert mem.num_outstanding_writes == 0
        assert mem.num_outstanding == 2

    def test_write_outstanding_tracking(self, mem):
        mem.submit(0x1000, True)
        assert mem.num_outstanding_writes == 1
        assert mem.num_outstanding_reads == 0

    def test_outstanding_decreases_on_completion(self, mem):
        mem.submit(0x1000, False)
        assert mem.num_outstanding == 1
        mem.drain()
        assert mem.num_outstanding == 0

    def test_same_address_fifo(self, mem):
        mem.submit(0x1000, False)
        mem.submit(0x1000, False)
        evs = mem.drain()
        assert len(evs) == 2
        assert evs[0].latency <= evs[1].latency


class TestMemoryCompletions:
    def test_read_completion(self, tmp_path):
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        mem.submit(0x1000, False)
        evs = mem.drain()
        assert len(evs) == 1
        assert evs[0].addr == 0x1000
        assert evs[0].latency > 0
        assert not evs[0].is_write

    def test_write_completion(self, tmp_path):
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        mem.submit(0x2000, True)
        evs = mem.drain()
        assert len(evs) == 1
        assert evs[0].addr == 0x2000
        assert evs[0].is_write

    def test_mixed_read_write(self, tmp_path):
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        for i in range(8):
            mem.submit(0x1000 + i * 64, False)
            mem.submit(0x2000 + i * 64, True)
        evs = mem.drain()
        assert sum(1 for e in evs if not e.is_write) == 8
        assert sum(1 for e in evs if e.is_write) == 8


class TestMemoryStats:
    @pytest.fixture
    def mem(self, tmp_path):
        m = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        for i in range(16):
            m.submit(0x1000 + i * 64, False)
        m.drain()
        return m

    def test_print_stats_creates_files(self, mem):
        mem.print_stats()
        assert mem.stats_json_path.exists()
        assert mem.stats_txt_path.exists()

    def test_get_stats_returns_dict(self, mem):
        stats = mem.get_stats()
        assert isinstance(stats, dict)
        assert "0" in stats

    def test_get_stats_reads_match(self, mem):
        stats = mem.get_stats()
        assert stats["0"]["num_reads_done"] == 16

    def test_get_stats_refresh_false_reuses_snapshot(self, mem):
        a = mem.get_stats()
        assert a["0"]["num_reads_done"] == 16
        b = mem.get_stats(refresh=False)
        assert a is b
        c = mem.get_stats(refresh=True)
        assert c is not a
        assert mem.get_stats(refresh=False) is c

    def test_reset_stats(self, mem):
        mem.reset_stats()
        stats = mem.get_stats()
        assert stats["0"]["num_reads_done"] == 0

    def test_reset_stats_invalidates_refresh_false_cache(self, mem):
        before = mem.get_stats()
        assert before["0"]["num_reads_done"] == 16
        mem.reset_stats()
        after = mem.get_stats(refresh=False)
        assert after is not before
        assert after["0"]["num_reads_done"] == 0


class TestMemoryContextManager:
    def test_with_statement(self, tmp_path):
        with Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path)) as mem:
            mem.submit(0x1000, False)
            mem.drain()
            assert not mem.busy


class TestDrain:
    @pytest.fixture
    def mem(self, tmp_path):
        return Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))

    def test_drain_empty(self, mem):
        assert mem.drain() == []

    def test_drain_completes_all(self, mem):
        for i in range(16):
            mem.submit(0x1000 + i * 64, False)
        assert mem.num_outstanding == 16
        evs = mem.drain()
        assert len(evs) == 16
        assert mem.num_outstanding == 0

    def test_drain_timeout(self, mem):
        for i in range(16):
            mem.submit(0x1000 + i * 64, False)
        with pytest.raises(RuntimeError, match="still busy"):
            mem.drain(max_cycles=1)


class TestReplay:
    def test_replay_basic(self, tmp_path):
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        trace = [(0x1000 + i * 64, False) for i in range(32)]
        total_cycles = mem.replay(trace)
        assert total_cycles > 0
        assert mem.num_outstanding == 0

    def test_replay_mixed(self, tmp_path):
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        trace = [(0x1000 + i * 64, i % 2 == 1) for i in range(20)]
        for addr, is_write in trace:
            mem.submit(addr, is_write)
        evs = mem.drain()
        assert sum(1 for e in evs if not e.is_write) == 10
        assert sum(1 for e in evs if e.is_write) == 10

    def test_replay_with_gap(self, tmp_path):
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        trace = [(0x1000 + i * 64, False) for i in range(32)]
        cycles_no_gap = mem.replay(trace)
        mem2 = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        cycles_with_gap = mem2.replay(trace, gap_cycles=50)
        assert cycles_with_gap > cycles_no_gap

    def test_replay_handles_backpressure(self, tmp_path):
        mem = Memory.from_config(
            "DDR4_8Gb_x8_2400", working_dir=str(tmp_path), frontend_queue=False
        )
        trace = [(0x1000 + i * 64, False) for i in range(100)]
        mem.replay(trace)
        assert not mem.busy

    def test_replay_raw_without_frontend(self, tmp_path):
        mem = Memory.from_config(
            "DDR4_8Gb_x8_2400", working_dir=str(tmp_path), frontend_queue=False
        )
        addr = 0x1000
        cycles = mem.replay([(addr, False), (addr, True)])
        assert cycles > 0
        assert not mem.busy

    def test_replay_outstanding_cap(self, tmp_path):
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path), outstanding_cap=8)
        trace = [(0x1000 + i * 64, False) for i in range(40)]
        cycles = mem.replay(trace)
        assert cycles > 0
        assert not mem.busy

    def test_replay_outstanding_cap_without_frontend(self, tmp_path):
        """Cap must bind try_admit; DRAM-queue rejects must not credit-wait."""
        mem = Memory.from_config(
            "DDR4_8Gb_x8_2400",
            working_dir=str(tmp_path),
            frontend_queue=False,
            outstanding_cap=4,
        )
        # More bursts than both the cap and the MC queue.
        trace = [(0x1000 + i * 64, False) for i in range(40)]
        cycles = mem.replay(trace)
        assert cycles > 0
        assert not mem.busy
        # Never admitted past the cap (replay drains; check via a fresh fill).
        mem2 = Memory.from_config(
            "DDR4_8Gb_x8_2400",
            working_dir=str(tmp_path),
            frontend_queue=False,
            outstanding_cap=4,
        )
        tags = [mem2.submit(0x2000 + i * 64, False) for i in range(8)]
        assert sum(t is not None for t in tags) == 4
        assert mem2.in_flight == 4

    def test_replay_timeout_raises(self, tmp_path):
        mem = Memory.from_config(
            "DDR4_8Gb_x8_2400", working_dir=str(tmp_path), frontend_queue=False
        )
        trace = [(0x1000 + i * 64, False) for i in range(mem.queue_size + 8)]
        with pytest.raises(RuntimeError, match="replay"):
            mem.replay(trace, max_cycles=1)

    def test_replay_generator(self, tmp_path):
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        trace = ((0x1000 + i * 64, False) for i in range(10))
        total = mem.replay(trace)
        assert total > 0
        assert mem.num_outstanding == 0

    def test_replay_with_tags(self, tmp_path):
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        for i in range(16):
            mem.submit(0x1000 + i * 64, False, tag=9000 + i)
        evs = mem.drain()
        assert sorted(e.tag for e in evs) == list(range(9000, 9016))

    def test_replay_mixed_tuple_lengths(self, tmp_path):
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        trace = [(0x1000, False), (0x1040, False, 7), (0x1080, False)]
        total = mem.replay(trace)
        assert total > 0
        assert mem.num_outstanding == 0


class TestLatencyTracker:
    def test_basic_collection(self, tmp_path):
        tracker = LatencyTracker()
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        for i in range(16):
            mem.submit(0x1000 + i * 64, False)
        tracker.add(mem.drain())
        assert tracker.num_reads == 16
        assert tracker.num_writes == 0

    def test_read_stats(self, tmp_path):
        tracker = LatencyTracker()
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        for i in range(32):
            mem.submit(0x1000 + i * 64, False)
        tracker.add(mem.drain())
        stats = tracker.read_stats
        assert stats.count == 32
        assert stats.avg > 0
        assert stats.min > 0
        assert stats.max >= stats.min
        assert stats.p50 >= stats.min
        assert stats.p99 >= stats.p50
        assert stats.p99 <= stats.max

    def test_write_stats(self, tmp_path):
        tracker = LatencyTracker()
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        for i in range(16):
            mem.submit(0x1000 + i * 64, True)
        tracker.add(mem.drain())
        stats = tracker.write_stats
        assert stats.count == 16
        assert stats.avg > 0

    def test_all_stats(self, tmp_path):
        tracker = LatencyTracker()
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        for i in range(20):
            mem.submit(0x1000 + i * 64, i % 2 == 1)
        tracker.add(mem.drain())
        assert tracker.all_stats.count == 20

    def test_reset(self, tmp_path):
        tracker = LatencyTracker()
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        mem.submit(0x1000, False)
        tracker.add(mem.drain())
        assert tracker.num_reads == 1
        tracker.reset()
        assert tracker.num_reads == 0
        assert tracker.read_stats.count == 0

    def test_summary(self, tmp_path):
        tracker = LatencyTracker()
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        for i in range(10):
            mem.submit(0x1000 + i * 64, i % 2 == 1)
        tracker.add(mem.drain())
        s = tracker.summary()
        assert "read(" in s
        assert "write(" in s

    def test_summary_empty(self):
        tracker = LatencyTracker()
        assert tracker.summary() == "no transactions"


class TestLatencyStats:
    def test_empty(self):
        stats = LatencyStats([])
        assert stats.count == 0
        assert stats.avg == 0.0
        assert stats.min == 0
        assert stats.max == 0
        assert stats.p50 == 0
        assert stats.p99 == 0

    def test_single_value(self):
        stats = LatencyStats([42])
        assert stats.count == 1
        assert stats.avg == 42.0
        assert stats.min == 42
        assert stats.max == 42
        assert stats.p50 == 42
        assert stats.p99 == 42

    def test_percentile_ordering(self):
        stats = LatencyStats(list(range(1, 101)))
        assert stats.p50 <= stats.p90 <= stats.p95 <= stats.p99

    def test_arbitrary_percentile(self):
        stats = LatencyStats(list(range(1, 1001)))
        assert stats.percentile(0.75) >= stats.percentile(0.25)

    def test_values_sorted(self):
        stats = LatencyStats([5, 3, 1, 4, 2])
        assert stats.values == [1, 2, 3, 4, 5]

    def test_repr(self):
        stats = LatencyStats([10, 20, 30])
        r = repr(stats)
        assert "n=3" in r
        assert "avg=" in r


class TestPythonic:
    """Pythonic surface: enum request types, generator completion consumption."""

    @staticmethod
    def _mem(tmp_path, **kw):
        return Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path), **kw)

    def test_request_type_enum(self):
        assert RequestType.READ != RequestType.WRITE
        assert bool(RequestType.WRITE) is True
        assert bool(RequestType.READ) is False

    def test_submit_accepts_request_type_and_bool(self, tmp_path):
        mem = self._mem(tmp_path)
        assert mem.submit(0x1000, RequestType.READ) is not None
        assert mem.submit(0x1000, RequestType.WRITE) is not None
        assert mem.submit(0x2000, True) is not None
        assert mem.submit(0x2000, False) is not None
        mem.drain()

    def test_replay_accepts_request_type(self, tmp_path):
        mem = self._mem(tmp_path)
        trace = [
            (0x1000 + i * 64, RequestType.READ if i % 2 else RequestType.WRITE) for i in range(20)
        ]
        cycles = mem.replay(trace)
        assert cycles > 0
        assert mem.num_outstanding == 0

    def test_completions_generator(self, tmp_path):
        mem = self._mem(tmp_path)
        for i in range(16):
            assert mem.submit(0x1000 + i * 64, RequestType.READ, tag=i) is not None
        completions = list(mem.completions())
        assert len(completions) == 16
        assert all(isinstance(c, Completion) for c in completions)
        assert [c.tag for c in completions] == list(range(16))
        assert all(c.is_write is False for c in completions)
        assert all(c.latency > 0 for c in completions)
        assert all(c.cycle > 0 for c in completions)
        assert mem.num_outstanding == 0

    def test_completions_includes_writes(self, tmp_path):
        mem = self._mem(tmp_path)
        for i in range(8):
            assert mem.submit(0x1000 + i * 64, RequestType.WRITE) is not None
        completions = list(mem.completions())
        assert len(completions) == 8
        assert all(c.is_write for c in completions)
        assert mem.num_outstanding == 0

    def test_get_stats(self, tmp_path):
        mem = self._mem(tmp_path)
        mem.submit(0x1000, RequestType.READ)
        mem.drain()
        stats = mem.get_stats()
        assert isinstance(stats, dict)
        assert "0" in stats
