import numpy as np
import pytest

from pydramsim3 import (
    LatencyStats,
    LatencyTracker,
    MemoryController,
    configs_dir,
    list_configs,
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

    def test_take_read_events_np(self, tmp_path):
        e = self._make(tmp_path)
        e.try_enqueue(0x1000, False)
        e.tick(500)
        addrs, lats, _tags = e.take_read_events_np()
        assert addrs.dtype == np.uint64
        assert addrs.tolist() == [0x1000]
        assert lats.tolist()[0] > 0
        # buffers cleared after take
        addrs, _, _ = e.take_read_events_np()
        assert len(addrs) == 0

    def test_take_write_events_np(self, tmp_path):
        e = self._make(tmp_path)
        e.try_enqueue(0x2000, True)
        e.tick(10)
        addrs, _, _ = e.take_write_events_np()
        assert addrs.tolist() == [0x2000]

    def test_try_enqueue_and_take_read_events(self, tmp_path):
        e = self._make(tmp_path)
        assert e.try_enqueue(0x1000, False)
        e.tick(500)
        addrs, lats, _tags = e.take_read_events()
        assert addrs == [0x1000]
        assert len(lats) == 1 and lats[0] > 0

    def test_take_events_clears_buffer(self, tmp_path):
        e = self._make(tmp_path)
        e.try_enqueue(0x1000, False)
        e.tick(500)
        addrs, _, _ = e.take_read_events()
        assert len(addrs) == 1
        addrs, _, _ = e.take_read_events()
        assert addrs == []

    def test_write_events(self, tmp_path):
        e = self._make(tmp_path)
        e.try_enqueue(0x2000, True)
        e.tick(10)
        addrs, _lats, _tags = e.take_write_events()
        assert addrs == [0x2000]

    def test_tag_roundtrip(self, tmp_path):
        e = self._make(tmp_path)
        assert e.try_enqueue(0x1000, False, tag=42)
        e.tick(500)
        addrs, _lats, tags = e.take_read_events()
        assert addrs == [0x1000]
        assert tags == [42]

    def test_tag_default_zero(self, tmp_path):
        e = self._make(tmp_path)
        assert e.try_enqueue(0x1000, False)
        e.tick(500)
        _, _, tags = e.take_read_events()
        assert tags == [0]

    def test_tags_fifo_same_address(self, tmp_path):
        e = self._make(tmp_path)
        e.try_enqueue(0x1000, False, tag=1)
        e.try_enqueue(0x1000, False, tag=2)
        e.try_enqueue(0x1000, False, tag=3)
        e.tick(1000)
        _, _, tags = e.take_read_events()
        # FIFO per address: completions arrive in submission order
        assert tags == [1, 2, 3]

    def test_backpressure_at_queue_size(self, tmp_path):
        e = self._make(tmp_path, collect=False)
        accepted = 0
        while e.try_enqueue(0x1000 + accepted * 64, False):
            accepted += 1
        assert accepted == e.queue_size
        assert not e.try_enqueue(0x9999, False)

    def test_tick_until_capacity_waits(self, tmp_path):
        e = self._make(tmp_path, collect=False)
        accepted = 0
        while e.try_enqueue(0x1000 + accepted * 64, False):
            accepted += 1
        assert accepted == e.queue_size
        # queue is now full; waiting must advance cycles and free a slot
        addr = 0x1000 + accepted * 64
        n = e.tick_until_capacity(addr, False)
        assert n > 0
        assert e.try_enqueue(addr, False)

    def test_tick_until_capacity_returns_zero_when_free(self, tmp_path):
        e = self._make(tmp_path)
        assert e.tick_until_capacity(0x1000, False) == 0

    def test_drain(self, tmp_path):
        e = self._make(tmp_path, collect=False)
        for i in range(16):
            assert e.try_enqueue(0x1000 + i * 64, False)
        assert e.num_outstanding() == 16
        cycles = e.drain(1_000_000)
        assert cycles > 0
        assert e.num_outstanding() == 0

    def test_drain_empty_returns_zero(self, tmp_path):
        e = self._make(tmp_path)
        assert e.drain(1000) == 0

    def test_set_collect_clears_events(self, tmp_path):
        e = self._make(tmp_path)
        e.try_enqueue(0x1000, False)
        e.tick(500)
        e.set_collect(False)
        addrs, _, _ = e.take_read_events()
        assert addrs == []

    def test_multiple_completions_ordered(self, tmp_path):
        e = self._make(tmp_path)
        for _i in range(4):
            e.try_enqueue(0x1000, False)
        e.tick(1000)
        addrs, lats, _tags = e.take_read_events()
        assert len(addrs) == 4
        # FIFO per address: latencies non-decreasing
        assert lats == sorted(lats)

    def test_write_buffer_backpressure_no_deadlock(self, tmp_path):
        """Regression: DRAMsim3 write callbacks fire one cycle after submit,
        so the write_buffer_ can stay full while the outstanding counter
        reads zero; waiting must key on DRAMsim3's own acceptance check."""
        e = self._make(tmp_path, collect=False)
        for i in range(300):
            while not e.try_enqueue(0x1000 + i * 64, True):
                e.tick_until_capacity(0x1000 + i * 64, True)
        e.drain(10_000_000)
        assert e.num_outstanding() == 0

    def test_sustained_mixed_replay_no_deadlock(self, tmp_path):
        """Regression: sustained mixed traffic used to busy-spin in Python."""
        mc = MemoryController.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        trace = [(0x1000 + i * 64, i % 2 == 1) for i in range(500)]
        cycles = mc.replay(trace)
        assert cycles > 0
        assert mc.num_outstanding == 0


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
    def _mc(tmp_path, **kw):
        return MemoryController.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path), **kw)

    def test_matches_replay_cycles(self, tmp_path):
        addrs, writes = self._make_trace(256)
        c1 = self._mc(tmp_path).replay([(int(a), bool(w)) for a, w in zip(addrs, writes)])
        c2 = self._mc(tmp_path).run_trace(addrs, writes)
        assert c1 == c2

    def test_cycle_counter_advances(self, tmp_path):
        mc = self._mc(tmp_path)
        addrs, writes = self._make_trace(16)
        cycles = mc.run_trace(addrs, writes)
        assert cycles > 0
        assert mc.current_cycle == cycles

    def test_callbacks_receive_events(self, tmp_path):
        reads, writes = [], []
        mc = self._mc(
            tmp_path,
            read_complete=lambda a, lat: reads.append(a),
            write_complete=lambda a, lat: writes.append(a),
        )
        addrs, wflags = self._make_trace(64)
        mc.run_trace(addrs, wflags)
        assert len(reads) == 32
        assert len(writes) == 32
        assert mc.num_outstanding == 0

    def test_empty_trace(self, tmp_path):
        mc = self._mc(tmp_path)
        a = np.zeros(0, dtype=np.uint64)
        w = np.zeros(0, dtype=bool)
        assert mc.run_trace(a, w) == 0

    def test_drain_false(self, tmp_path):
        mc = self._mc(tmp_path)
        addrs, writes = self._make_trace(16, write_odd=False)
        mc.run_trace(addrs, writes, max_drain_cycles=0)
        assert mc.num_outstanding > 0
        mc.drain()
        assert mc.num_outstanding == 0

    def test_length_mismatch(self, tmp_path):
        mc = self._mc(tmp_path)
        with pytest.raises(ValueError):
            mc.run_trace(np.zeros(4, dtype=np.uint64), np.zeros(3, dtype=bool))

    def test_accepts_lists(self, tmp_path):
        mc = self._mc(tmp_path)
        mc.run_trace([0x1000, 0x1040, 0x1080], [False, False, False])
        mc.drain()
        assert mc.num_outstanding == 0

    def test_strided_views(self, tmp_path):
        mc = self._mc(tmp_path)
        addrs, writes = self._make_trace(128, write_odd=False)
        cycles = mc.run_trace(addrs[::2], writes[::2])
        assert cycles > 0
        mc.drain()
        assert mc.num_outstanding == 0

    def test_gap_cycles(self, tmp_path):
        addrs, writes = self._make_trace(16)
        c0 = self._mc(tmp_path).run_trace(addrs, writes)
        c1 = self._mc(tmp_path).run_trace(addrs, writes, gap_cycles=50)
        assert c1 > c0


class TestMemoryControllerTags:
    """Request tags (gem5 PacketPtr analog) flowing through the controller."""

    @pytest.fixture
    def mc(self, tmp_path):
        return MemoryController.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))

    def test_tagged_callback_receives_tag(self, tmp_path):
        results = []
        mc = MemoryController.from_config(
            "DDR4_8Gb_x8_2400",
            working_dir=str(tmp_path),
            read_complete=lambda addr, lat, tag: results.append((addr, lat, tag)),
        )
        assert mc.submit(0x1000, False, tag=7)
        mc.run(500)
        assert results == [(0x1000, results[0][1], 7)]

    def test_same_address_distinct_tags(self, tmp_path):
        results = []
        mc = MemoryController.from_config(
            "DDR4_8Gb_x8_2400",
            working_dir=str(tmp_path),
            read_complete=lambda addr, lat, tag: results.append((addr, tag)),
        )
        mc.submit(0x1000, False, tag=11)
        mc.submit(0x1000, False, tag=22)
        mc.run(1000)
        assert [t for _, t in results] == [11, 22]

    def test_legacy_two_arg_callback_still_works(self, mc):
        results = []
        mc.set_callbacks(read_complete=lambda addr, lat: results.append((addr, lat)))
        mc.submit(0x1000, False)
        mc.run(500)
        assert len(results) == 1
        assert results[0][0] == 0x1000
        assert results[0][1] > 0

    def test_untagged_submit_reports_zero(self, tmp_path):
        results = []
        mc = MemoryController.from_config(
            "DDR4_8Gb_x8_2400",
            working_dir=str(tmp_path),
            write_complete=lambda addr, lat, tag: results.append((addr, tag)),
        )
        mc.submit(0x2000, True)
        mc.run(10)
        assert results == [(0x2000, 0)]


# ---------------------------------------------------------------------------
# Different configs
# ---------------------------------------------------------------------------


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
        mc = MemoryController.from_config(config_name, working_dir=str(tmp_path))
        assert mc.clock_period > 0
        assert mc.burst_size > 0
        mc.run(10)


# ---------------------------------------------------------------------------
# MemoryController — gem5-aligned flow control
# ---------------------------------------------------------------------------


class TestMemoryControllerConstruction:
    def test_from_config(self, tmp_path):
        mc = MemoryController.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        assert mc.clock_period > 0
        assert mc.queue_size == 32
        assert mc.burst_size == 64

    def test_from_config_invalid(self):
        with pytest.raises(FileNotFoundError, match="not found"):
            MemoryController.from_config("NONEXISTENT_CONFIG")

    def test_burst_size_validation(self, tmp_path):
        with pytest.raises(ValueError, match="does not match"):
            MemoryController.from_config(
                "DDR4_8Gb_x8_2400", working_dir=str(tmp_path), burst_size=128
            )

    def test_burst_size_valid(self, tmp_path):
        mc = MemoryController.from_config(
            "DDR4_8Gb_x8_2400", working_dir=str(tmp_path), burst_size=64
        )
        assert mc.burst_size == 64

    def test_repr(self, tmp_path):
        mc = MemoryController.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        r = repr(mc)
        assert "DDR4_8Gb_x8_2400.ini" in r
        assert "outstanding=0" in r


class TestMemoryControllerFlowControl:
    @pytest.fixture
    def mc(self, tmp_path):
        return MemoryController.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))

    def test_submit_accepted(self, mc):
        assert mc.submit(0x1000, False) is True
        assert mc.num_outstanding == 1

    def test_backpressure_at_queue_size(self, mc):
        accepted = 0
        while mc.submit(0x1000 + accepted * 64, False):
            accepted += 1
        assert accepted == mc.queue_size

    def test_rejected_after_full(self, mc):
        for i in range(mc.queue_size):
            mc.submit(0x1000 + i * 64, False)
        assert mc.submit(0x9999, False) is False

    def test_retry_pending_set_on_reject(self, mc):
        for i in range(mc.queue_size):
            mc.submit(0x1000 + i * 64, False)
        mc.submit(0x9999, False)
        assert mc.retry_pending is True

    def test_retry_blocks_further_submits(self, mc):
        for i in range(mc.queue_size):
            mc.submit(0x1000 + i * 64, False)
        mc.submit(0x9999, False)  # rejected, sets retry
        # Even if we tick to free space, submit stays blocked until retry clears
        mc.tick()
        assert mc.submit(0xAAAA, False) is False

    def test_retry_cleared_by_tick(self, mc):
        for i in range(mc.queue_size):
            mc.submit(0x1000 + i * 64, False)
        mc.submit(0x9999, False)
        assert mc.retry_pending is True
        # Tick until completions free space and retry clears
        for _ in range(200):
            mc.tick()
            if not mc.retry_pending:
                break
        assert mc.retry_pending is False

    def test_submit_after_retry_cleared(self, mc):
        for i in range(mc.queue_size):
            mc.submit(0x1000 + i * 64, False)
        mc.submit(0x9999, False)
        for _ in range(200):
            mc.tick()
            if not mc.retry_pending:
                break
        assert mc.submit(0xBBBB, False) is True


class TestMemoryControllerOutstanding:
    @pytest.fixture
    def mc(self, tmp_path):
        return MemoryController.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))

    def test_read_outstanding_tracking(self, mc):
        mc.submit(0x1000, False)
        mc.submit(0x2000, False)
        assert mc.num_outstanding_reads == 2
        assert mc.num_outstanding_writes == 0
        assert mc.num_outstanding == 2

    def test_write_outstanding_tracking(self, mc):
        mc.submit(0x1000, True)
        assert mc.num_outstanding_writes == 1
        assert mc.num_outstanding_reads == 0

    def test_outstanding_decreases_on_completion(self, mc):
        mc.submit(0x1000, False)
        assert mc.num_outstanding == 1
        mc.run(200)
        assert mc.num_outstanding == 0

    def test_same_address_fifo(self, mc):
        results = []
        mc._user_read_cb = lambda addr, lat: results.append((addr, lat))
        mc.submit(0x1000, False)
        mc.submit(0x1000, False)
        assert mc.num_outstanding_reads == 2
        mc.run(200)
        assert len(results) == 2
        # FIFO: first submit gets shorter latency
        assert results[0][1] <= results[1][1]


class TestMemoryControllerCallbacks:
    def test_read_complete_callback(self, tmp_path):
        results = []
        mc = MemoryController.from_config(
            "DDR4_8Gb_x8_2400",
            working_dir=str(tmp_path),
            read_complete=lambda addr, lat: results.append((addr, lat)),
        )
        mc.submit(0x1000, False)
        mc.run(200)
        assert len(results) == 1
        assert results[0][0] == 0x1000
        assert results[0][1] > 0

    def test_write_complete_callback(self, tmp_path):
        results = []
        mc = MemoryController.from_config(
            "DDR4_8Gb_x8_2400",
            working_dir=str(tmp_path),
            write_complete=lambda addr, lat: results.append((addr, lat)),
        )
        mc.submit(0x2000, True)
        mc.run(200)
        assert len(results) == 1
        assert results[0][0] == 0x2000
        assert results[0][1] > 0

    def test_latency_is_positive(self, tmp_path):
        latencies = []
        mc = MemoryController.from_config(
            "DDR4_8Gb_x8_2400",
            working_dir=str(tmp_path),
            read_complete=lambda addr, lat: latencies.append(lat),
        )
        for i in range(16):
            mc.submit(0x1000 + i * 64, False)
        mc.run(500)
        assert all(lat > 0 for lat in latencies)

    def test_no_callback_no_crash(self, tmp_path):
        mc = MemoryController.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        mc.submit(0x1000, False)
        mc.run(200)
        assert mc.num_outstanding == 0


class TestMemoryControllerSimulation:
    @pytest.fixture
    def mc(self, tmp_path):
        return MemoryController.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))

    def test_tick_advances_cycle(self, mc):
        assert mc.current_cycle == 0
        mc.tick()
        assert mc.current_cycle == 1
        mc.run(99)
        assert mc.current_cycle == 100

    def test_run_returns_count(self, mc):
        assert mc.run(50) == 50

    def test_mixed_read_write_traffic(self, tmp_path):
        reads = []
        writes = []
        mc = MemoryController.from_config(
            "DDR4_8Gb_x8_2400",
            working_dir=str(tmp_path),
            read_complete=lambda a, lat: reads.append(a),
            write_complete=lambda a, lat: writes.append(a),
        )
        for i in range(8):
            mc.submit(0x1000 + i * 64, False)
            mc.submit(0x2000 + i * 64, True)
        mc.run(500)
        assert len(reads) == 8
        assert len(writes) == 8

    def test_sustained_traffic_with_backpressure(self, tmp_path):
        completed = []
        mc = MemoryController.from_config(
            "DDR4_8Gb_x8_2400",
            working_dir=str(tmp_path),
            read_complete=lambda a, lat: completed.append(a),
        )
        submitted = 0
        target = 100
        for _cycle in range(5000):
            if (
                submitted < target
                and not mc.retry_pending
                and mc.submit(0x1000 + submitted * 64, False)
            ):
                submitted += 1
            mc.tick()
            if submitted == target and mc.num_outstanding == 0:
                break
        assert len(completed) == target


class TestMemoryControllerStats:
    @pytest.fixture
    def mc(self, tmp_path):
        m = MemoryController.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        for i in range(16):
            m.submit(0x1000 + i * 64, False)
        m.run(2000)
        return m

    def test_print_stats_creates_files(self, mc):
        mc.print_stats()
        assert mc.stats_json_path.exists()
        assert mc.stats_txt_path.exists()

    def test_get_stats_returns_dict(self, mc):
        stats = mc.get_stats()
        assert isinstance(stats, dict)
        assert "0" in stats

    def test_get_stats_reads_match(self, mc):
        stats = mc.get_stats()
        assert stats["0"]["num_reads_done"] == 16

    def test_reset_stats(self, mc):
        mc.reset_stats()
        stats = mc.get_stats()
        assert stats["0"]["num_reads_done"] == 0


class TestMemoryControllerContextManager:
    def test_with_statement(self, tmp_path):
        with MemoryController.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path)) as mc:
            mc.submit(0x1000, False)
            mc.run(100)
            assert mc.current_cycle == 100


# ---------------------------------------------------------------------------
# MemoryController — drain / replay helpers
# ---------------------------------------------------------------------------


class TestDrain:
    @pytest.fixture
    def mc(self, tmp_path):
        return MemoryController.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))

    def test_drain_empty(self, mc):
        assert mc.drain() == 0

    def test_drain_completes_all(self, mc):
        for i in range(16):
            mc.submit(0x1000 + i * 64, False)
        assert mc.num_outstanding == 16
        cycles = mc.drain()
        assert cycles > 0
        assert mc.num_outstanding == 0

    def test_drain_timeout(self, mc):
        for i in range(16):
            mc.submit(0x1000 + i * 64, False)
        with pytest.raises(RuntimeError, match="still outstanding"):
            mc.drain(max_cycles=1)


class TestReplay:
    def test_replay_basic(self, tmp_path):
        results = []
        mc = MemoryController.from_config(
            "DDR4_8Gb_x8_2400",
            working_dir=str(tmp_path),
            read_complete=lambda a, lat: results.append((a, lat)),
        )
        trace = [(0x1000 + i * 64, False) for i in range(32)]
        total_cycles = mc.replay(trace)
        assert total_cycles > 0
        assert len(results) == 32
        assert mc.num_outstanding == 0

    def test_replay_mixed(self, tmp_path):
        reads = []
        writes = []
        mc = MemoryController.from_config(
            "DDR4_8Gb_x8_2400",
            working_dir=str(tmp_path),
            read_complete=lambda a, lat: reads.append(a),
            write_complete=lambda a, lat: writes.append(a),
        )
        trace = [(0x1000 + i * 64, i % 2 == 1) for i in range(20)]
        mc.replay(trace)
        assert len(reads) == 10
        assert len(writes) == 10

    def test_replay_with_gap(self, tmp_path):
        mc = MemoryController.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        trace = [(0x1000 + i * 64, False) for i in range(32)]
        cycles_no_gap = mc.replay(trace)

        mc2 = MemoryController.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        cycles_with_gap = mc2.replay(trace, gap_cycles=50)
        assert cycles_with_gap > cycles_no_gap

    def test_replay_handles_backpressure(self, tmp_path):
        results = []
        mc = MemoryController.from_config(
            "DDR4_8Gb_x8_2400",
            working_dir=str(tmp_path),
            read_complete=lambda a, lat: results.append(a),
        )
        trace = [(0x1000 + i * 64, False) for i in range(100)]
        mc.replay(trace)
        assert len(results) == 100

    def test_replay_generator(self, tmp_path):
        mc = MemoryController.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        trace = ((0x1000 + i * 64, False) for i in range(10))
        total = mc.replay(trace)
        assert total > 0
        assert mc.num_outstanding == 0

    def test_replay_with_tags(self, tmp_path):
        results = []
        mc = MemoryController.from_config(
            "DDR4_8Gb_x8_2400",
            working_dir=str(tmp_path),
            read_complete=lambda addr, lat, tag: results.append((addr, tag)),
        )
        trace = [(0x1000 + i * 64, False, 9000 + i) for i in range(16)]
        mc.replay(trace)
        assert sorted(t for _, t in results) == list(range(9000, 9016))

    def test_replay_mixed_tuple_lengths(self, tmp_path):
        mc = MemoryController.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        trace = [(0x1000, False), (0x1040, False, 7), (0x1080, False)]
        total = mc.replay(trace)
        assert total > 0
        assert mc.num_outstanding == 0


# ---------------------------------------------------------------------------
# LatencyTracker
# ---------------------------------------------------------------------------


class TestLatencyTracker:
    def test_basic_collection(self, tmp_path):
        tracker = LatencyTracker()
        mc = MemoryController.from_config(
            "DDR4_8Gb_x8_2400",
            working_dir=str(tmp_path),
            read_complete=tracker.on_read,
            write_complete=tracker.on_write,
        )
        trace = [(0x1000 + i * 64, False) for i in range(16)]
        mc.replay(trace)
        assert tracker.num_reads == 16
        assert tracker.num_writes == 0

    def test_read_stats(self, tmp_path):
        tracker = LatencyTracker()
        mc = MemoryController.from_config(
            "DDR4_8Gb_x8_2400",
            working_dir=str(tmp_path),
            read_complete=tracker.on_read,
        )
        mc.replay([(0x1000 + i * 64, False) for i in range(32)])
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
        mc = MemoryController.from_config(
            "DDR4_8Gb_x8_2400",
            working_dir=str(tmp_path),
            write_complete=tracker.on_write,
        )
        mc.replay([(0x1000 + i * 64, True) for i in range(16)])
        stats = tracker.write_stats
        assert stats.count == 16
        assert stats.avg > 0

    def test_all_stats(self, tmp_path):
        tracker = LatencyTracker()
        mc = MemoryController.from_config(
            "DDR4_8Gb_x8_2400",
            working_dir=str(tmp_path),
            read_complete=tracker.on_read,
            write_complete=tracker.on_write,
        )
        mc.replay([(0x1000 + i * 64, i % 2 == 1) for i in range(20)])
        assert tracker.all_stats.count == 20

    def test_reset(self, tmp_path):
        tracker = LatencyTracker()
        mc = MemoryController.from_config(
            "DDR4_8Gb_x8_2400",
            working_dir=str(tmp_path),
            read_complete=tracker.on_read,
        )
        mc.replay([(0x1000, False)])
        assert tracker.num_reads == 1
        tracker.reset()
        assert tracker.num_reads == 0
        assert tracker.read_stats.count == 0

    def test_summary(self, tmp_path):
        tracker = LatencyTracker()
        mc = MemoryController.from_config(
            "DDR4_8Gb_x8_2400",
            working_dir=str(tmp_path),
            read_complete=tracker.on_read,
            write_complete=tracker.on_write,
        )
        mc.replay([(0x1000 + i * 64, i % 2 == 1) for i in range(10)])
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
