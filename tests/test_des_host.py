"""DES-facing Memory API: submit / wait / pull."""

import pytest

from pydramsim3 import Completion, DebugState, Memory, RequestType, configs_dir
from pydramsim3._dramsim3 import SimEngine


def _engine(tmp_path, collect=True):
    cfg = str(configs_dir() / "DDR4_8Gb_x8_2400.ini")
    return SimEngine(cfg, str(tmp_path), collect)


class TestTickUntilCompletion:
    def test_stops_at_first_completion(self, tmp_path):
        e = _engine(tmp_path)
        assert e.try_admit(0x1000, False, tag=1)
        n = e.advance_until_completion()
        assert n > 0
        assert e.current_cycle == n
        addrs, lats, tags, cycles = e.take_read_completions()
        assert addrs == [0x1000]
        assert tags == [1]
        assert lats[0] > 0
        assert cycles == [n]
        # One-by-one tick must match the DES timestamp.
        e2 = _engine(tmp_path)
        e2.try_admit(0x1000, False, tag=1)
        for _ in range(n):
            e2.tick(1)
        assert e2.current_cycle == n
        _, lats2, _, cycles2 = e2.take_read_completions()
        assert lats2 == lats
        assert cycles2 == cycles

    def test_idle_returns_zero(self, tmp_path):
        e = _engine(tmp_path)
        assert e.advance_until_completion() == 0

    def test_write_completion(self, tmp_path):
        e = _engine(tmp_path)
        e.try_admit(0x2000, True, tag=7)
        n = e.advance_until_completion()
        assert n > 0
        addrs, _, tags, cycles = e.take_write_completions()
        assert addrs == [0x2000]
        assert tags == [7]
        assert cycles == [n]

    def test_advance_to_deadline(self, tmp_path):
        e = _engine(tmp_path)
        e.try_admit(0x1000, False)
        n = e.advance_to(5, stop_on_completion=False)
        assert n == 5
        assert e.current_cycle == 5
        # Read has not completed in 5 cycles on DDR4-2400.
        addrs, _, _, _ = e.take_read_completions()
        assert addrs == []

    def test_advance_to_stops_on_completion(self, tmp_path):
        e = _engine(tmp_path)
        e.try_admit(0x1000, False)
        n = e.advance_to(10_000, stop_on_completion=True)
        assert 0 < n < 10_000
        addrs, _, _, cycles = e.take_read_completions()
        assert len(addrs) == 1
        assert cycles == [n]


class TestTagQuota:
    def test_stops_when_logical_request_done(self, tmp_path):
        e = _engine(tmp_path)
        # Two bursts, same tag: stop after both complete, not the first.
        e.park(0x1000, False, tag=7)
        e.park(0x1040, False, tag=7)
        e.set_tag_quota(7, 2)
        n = e.advance_until(10_000_000, True)
        assert n > 0
        addrs, _, tags, _ = e.take_read_completions()
        assert tags == [7, 7]
        assert len(addrs) == 2
        assert e.in_flight == 0

    def test_stops_on_first_finished_tag_not_all_traffic(self, tmp_path):
        e = _engine(tmp_path)
        e.park(0x1000, False, tag=1)
        e.park(0x2000, False, tag=2)
        e.park(0x2040, False, tag=2)
        e.set_tag_quota(1, 1)
        e.set_tag_quota(2, 2)
        e.advance_until(10_000_000, True)
        _, _, tags, _ = e.take_read_completions()
        # Tag 1 is one burst; must not have drained tag 2's second burst
        # as a requirement — at least tag 1 is present.  Tag 2 may share
        # the same ClockTick.
        assert 1 in tags
        assert tags.count(1) == 1


class TestFrontendQueue:
    def test_park_beyond_queue_size(self, tmp_path):
        e = _engine(tmp_path)
        n_submit = e.queue_size + 16
        for i in range(n_submit):
            e.park(0x1000 + i * 64, False, tag=i + 1)
        assert e.frontend_size > 0
        # Single-channel DDR4: the read queue fills at trans_queue_size.
        assert e.num_outstanding == e.queue_size
        e.drain()
        addrs, _, tags, _ = e.take_read_completions()
        assert len(addrs) == n_submit
        assert tags == list(range(1, n_submit + 1))
        assert e.frontend_size == 0
        assert e.num_outstanding == 0

    def test_latency_includes_queue_wait(self, tmp_path):
        e = _engine(tmp_path)
        # Fill the controller, then park one extra.
        for i in range(e.queue_size):
            e.park(0x1000 + i * 64, False, tag=i)
        e.park(0x9000, False, tag=99)
        assert e.frontend_size >= 1
        e.drain()
        _, lats, tags, _ = e.take_read_completions()
        parked = lats[tags.index(99)]
        first = lats[tags.index(0)]
        assert parked >= first

    def test_in_flight_includes_frontend(self, tmp_path):
        e = _engine(tmp_path)
        n_submit = e.queue_size + 8
        for i in range(n_submit):
            e.park(0x1000 + i * 64, False, tag=i)
        assert e.in_flight == n_submit
        assert e.in_flight == e.num_outstanding + e.frontend_size
        e.drain()
        assert e.in_flight == 0

    def test_hol_read_bypasses_blocked_writes(self, tmp_path):
        """A blocked write at the frontend head must not stall a later read.

        Write callbacks fire one cycle after AddTransaction while the write
        buffer can stay full; HOL bypass admits the read into the free
        read queue.
        """
        e = _engine(tmp_path)
        n = e.queue_size + 8
        for i in range(n):
            e.park(0x1000 + i * 64, True, tag=i)
        e.park(0x9000, False, tag=999)
        assert e.frontend_size > 0
        e.tick(2)
        assert e.num_outstanding_reads == 1
        assert e.frontend_size > 0

    def test_park_range_matches_repeated_park(self, tmp_path):
        def drain_tags(use_range):
            e = _engine(tmp_path)
            if use_range:
                e.park_range(0x1000, 24, 64, False, tag=3)
            else:
                for i in range(24):
                    e.park(0x1000 + i * 64, False, tag=3)
            e.drain()
            addrs, _, tags, cycles = e.take_read_completions()
            return list(addrs), list(tags), list(cycles)

        assert drain_tags(True) == drain_tags(False)

    def test_advance_by_matches_advance_until(self, tmp_path):
        e = _engine(tmp_path)
        e.park(0x1000, False, tag=1)
        n = e.advance_by(10_000, True)
        addrs, _, tags, cycles = e.take_read_completions()
        e2 = _engine(tmp_path)
        e2.park(0x1000, False, tag=1)
        n2 = e2.advance_until(e2.current_cycle + 10_000, True)
        addrs2, _, tags2, cycles2 = e2.take_read_completions()
        assert n == n2
        assert addrs == addrs2
        assert tags == tags2
        assert cycles == cycles2

    def test_hbm_full_channel_does_not_stall_other(self, tmp_path):
        cfg = str(configs_dir() / "HBM1_4Gb_x128.ini")
        e = SimEngine(cfg, str(tmp_path), True)
        # 8 channels * queue_size reads, plus overflow to park on the frontend.
        n = 8 * e.queue_size + 64
        e.park_range(0x1000, n, 64, False, tag=1)
        parked = e.frontend_size
        assert parked > 0
        # Write queue is independent of a full read queue.  The write
        # address must not alias an in-flight read: DRAMSim3 deadlocks
        # if a posted write shares a pending read's address.
        wr = 0x1000 + n * 64 + (1 << 20)
        e.park(wr, True, tag=9999)
        assert e.num_outstanding_writes == 1
        assert e.frontend_size == parked

    def test_many_parked_reads_all_complete(self, tmp_path):
        cfg = str(configs_dir() / "HBM1_4Gb_x128.ini")
        e = SimEngine(cfg, str(tmp_path), True)
        n = 4096
        e.park_range(0x1000, n, 64, False, tag=1)
        e.set_tag_quota(1, n)
        e.drain()
        addrs, _, tags, _ = e.take_read_completions()
        assert len(addrs) == n
        assert tags == [1] * n
        assert e.frontend_size == 0
        assert e.in_flight == 0

    def test_outstanding_cap_blocks_then_partial_range(self, tmp_path):
        e = _engine(tmp_path)
        cap = e.queue_size
        e.outstanding_cap = cap
        assert e.outstanding_cap == cap
        parked = e.park_range(0x1000, cap + 40, 64, False, tag=1)
        assert parked == cap
        assert e.in_flight == cap
        assert e.park(0x9000, False, tag=99) is False
        extra = e.park_range(0x2000, 16, 64, False, tag=2)
        assert extra == 0
        e.drain()
        addrs, _, tags, _ = e.take_read_completions()
        assert len(addrs) == cap
        assert tags == [1] * cap
        parked2 = e.park_range(0x2000, 16, 64, False, tag=2)
        assert parked2 == 16
        e.drain()
        addrs, _, tags, _ = e.take_read_completions()
        assert tags == [2] * 16

    def test_read_then_full_write_buffer_does_not_deadlock(self, tmp_path):
        """Posted writes must not starve a same-address in-flight read.

        DRAMSim3 fills a per-channel write buffer of trans_queue_size and
        then only drains writes.  A head write to an addr with pending_rd
        aborts that drain, so the read never issues.  The wrapper must
        hold the aliasing write in the frontend until the read completes.
        """
        cfg = str(configs_dir() / "HBM1_4Gb_x128.ini")
        e = SimEngine(cfg, str(tmp_path), True)
        e.outstanding_cap = 0
        addr = 0x1000
        assert e.try_admit(addr, False, tag=1)
        # 32 same-channel writes (stride 8KB keeps HBM channel bits fixed);
        # the first aliases the in-flight read.
        for i in range(32):
            assert e.park(addr + i * (1 << 13), True, tag=2)
        assert e.num_outstanding_reads == 1
        assert e.frontend_blocked_writes == 1
        assert e.num_outstanding_writes == 31
        assert e.frontend_size == 1
        e.drain(1_000_000)
        assert e.in_flight == 0

    def test_advance_until_in_flight_below(self, tmp_path):
        e = _engine(tmp_path)
        cap = e.queue_size
        e.outstanding_cap = cap
        e.park_range(0x1000, cap, 64, False, tag=1)
        n = e.advance_until_in_flight_below(cap)
        assert n > 0
        assert e.in_flight < cap

    def test_outstanding_cap_keeps_hol_bypass(self, tmp_path):
        """A global credit cap must not serialize already-queued channels."""
        e = _engine(tmp_path)
        n = e.queue_size + 8
        e.outstanding_cap = n + 1
        for i in range(n):
            e.park(0x1000 + i * 64, True, tag=i)
        assert e.park(0x9000, False, tag=999)
        e.tick(2)
        assert e.num_outstanding_reads == 1
        assert e.frontend_size > 0


class TestMemoryApi:
    def test_des_loop_matches_tick_one(self, tmp_path):
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        tag = mem.submit(0x1000, False)
        evs = mem.wait()
        assert len(evs) == 1
        ev = evs[0]
        assert ev.addr == 0x1000
        assert ev.tag == tag
        assert ev.is_write is False
        assert ev.cycle == mem.current_cycle
        assert ev.latency > 0

    def test_submit_always_succeeds_with_frontend(self, tmp_path):
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        tags = [mem.submit(0x1000 + i * 64, False) for i in range(mem.queue_size + 8)]
        assert None not in tags
        assert mem.frontend_size > 0
        evs = mem.drain()
        assert len(evs) == mem.queue_size + 8

    def test_submit_range(self, tmp_path):
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        assert mem.submit_range(0x1000, 12, 64, False, tag=5) == 12
        evs = mem.drain()
        assert [e.tag for e in evs] == [5] * 12
        assert [e.addr for e in evs] == [0x1000 + i * 64 for i in range(12)]

    def test_hardware_outstanding_cap_hbm1(self, tmp_path):
        mem = Memory.from_config("HBM1_4Gb_x128", working_dir=str(tmp_path), outstanding_cap="hw")
        assert mem.num_channels == 8
        assert mem.queue_size == 32
        assert mem.outstanding_cap == 8 * 32 + 8
        n = mem.outstanding_cap + 64
        assert mem.submit_range(0x1000, n, 64, False, tag=1) == mem.outstanding_cap
        assert mem.in_flight == mem.outstanding_cap
        assert mem.submit(0x9000, False) is None
        evs = mem.drain()
        assert len(evs) == mem.outstanding_cap
        assert mem.submit_range(0x2000, 16, 64, False, tag=2) == 16
        assert mem.drain()

    def test_lockstep_tick_leaves_completions(self, tmp_path):
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        mem.submit(0x1000, False, tag=1)
        n = mem.advance_until_completion()
        assert n > 0
        assert not mem.busy
        assert mem.pull()[0].tag == 1
        assert mem.pull() == []

    def test_advance_by_stops_on_tag_quota(self, tmp_path):
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        mem.submit(0x1000, False, tag=7)
        mem.set_tag_quota(7, 1)
        n = mem.advance_by(10_000, stop_on_tag_done=True)
        assert 0 < n < 10_000
        assert mem.pull()[0].tag == 7

    def test_advance_until_in_flight_below_frees_credit(self, tmp_path):
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path), outstanding_cap=8)
        assert mem.submit_range(0x1000, 8, 64, False, tag=1) == 8
        assert mem.in_flight == 8
        n = mem.advance_until_in_flight_below(8)
        assert n > 0
        assert mem.in_flight < 8
        mem.drain()

    def test_advance_until_accept_when_full(self, tmp_path):
        mem = Memory.from_config(
            "DDR4_8Gb_x8_2400", working_dir=str(tmp_path), frontend_queue=False
        )
        accepted = 0
        while mem.submit(0x1000 + accepted * 64, False) is not None:
            accepted += 1
        assert accepted == mem.queue_size
        assert mem.advance_until_accept(0x9000, False) > 0
        assert mem.submit(0x9000, False) is not None
        mem.drain()

    def test_submit_range_zero_when_cap_full(self, tmp_path):
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path), outstanding_cap=8)
        assert mem.submit_range(0x1000, 8, 64, False, tag=1) == 8
        assert mem.submit_range(0x2000, 4, 64, False, tag=2) == 0
        mem.drain()
        assert mem.submit_range(0x2000, 4, 64, False, tag=2) == 4

    def test_no_frontend_independent_reject(self, tmp_path):
        mem = Memory.from_config(
            "DDR4_8Gb_x8_2400", working_dir=str(tmp_path), frontend_queue=False
        )
        accepted = 0
        while mem.submit(0x1000 + accepted * 64, False) is not None:
            accepted += 1
        assert accepted == mem.queue_size
        # After a completion frees a slot, a later submit succeeds.
        mem.wait()
        assert mem.submit(0x9000, False) is not None

    def test_completions_iterator(self, tmp_path):
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        mem.submit(0x1000, RequestType.READ)
        mem.submit(0x2000, RequestType.WRITE)
        got = list(mem.completions())
        assert len(got) == 2
        assert {c.addr for c in got} == {0x1000, 0x2000}
        assert not mem.busy

    def test_replay_frontend(self, tmp_path):
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        trace = [(0x1000 + i * 64, i % 2 == 1) for i in range(64)]
        cycles = mem.replay(trace)
        assert cycles > 0
        assert not mem.busy

    def test_replay_without_frontend(self, tmp_path):
        mem = Memory.from_config(
            "DDR4_8Gb_x8_2400", working_dir=str(tmp_path), frontend_queue=False
        )
        n = mem.queue_size + 16
        trace = [(0x1000 + i * 64, False) for i in range(n)]
        cycles = mem.replay(trace)
        assert cycles > 0
        assert not mem.busy

    def test_drain_timeout_raises(self, tmp_path):
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        mem.submit(0x1000, False)
        with pytest.raises(RuntimeError, match="drain"):
            mem.drain(max_cycles=1)

    def test_wait_timeout_raises(self, tmp_path):
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        mem.submit(0x1000, False)
        with pytest.raises(RuntimeError, match="in_flight=") as ei:
            mem.wait(max_cycles=1)
        msg = str(ei.value)
        assert msg.startswith("wait:")
        assert "unmatched=" in msg

    def test_unbounded_advance_until_caps(self, tmp_path):
        cfg = str(configs_dir() / "HBM1_4Gb_x128.ini")
        e = SimEngine(cfg, str(tmp_path), True)
        assert e.try_admit(0x1000, False, tag=1)
        n = e.advance_until((1 << 64) - 1, True, 5)
        assert n == 5
        assert e.in_flight == 1
        mem = Memory.from_config("HBM1_4Gb_x128", working_dir=str(tmp_path))
        mem.submit(0x1000, False)
        with pytest.raises(RuntimeError, match="advance_until"):
            mem.advance_until(None, max_cycles=1)

    def test_memory_size_and_channel_of(self, tmp_path):
        mem = Memory.from_config("HBM1_4Gb_x128", working_dir=str(tmp_path))
        assert mem.num_channels == 8
        assert mem.memory_size == 8 * 512 * (1 << 20)
        ch = mem.channel_of(0x1000)
        assert 0 <= ch < 8
        assert mem.will_accept(0x1000, False)
        st = mem.debug_state
        assert isinstance(st, DebugState)
        assert st.in_flight == 0
        assert st["in_flight"] == 0
        assert st["unmatched_callbacks"] == 0
        assert mem.unmatched_callbacks == 0
        assert mem.frontend_blocked_writes == 0

    def test_mixed_completion_order_matches_tick_one(self, tmp_path):
        def order_via_tick():
            e = _engine(tmp_path)
            for i in range(8):
                assert e.try_admit(0x1000 + i * 64, i % 2 == 1, tag=i)
            seen = []
            while e.in_flight > 0:
                e.tick(1)
                addrs, _, tags, _, writes = e.take_completions()
                for addr, tag, is_wr in zip(addrs, tags, writes):
                    seen.append((addr, tag, bool(is_wr)))
            return seen

        mem = Memory.from_config(
            "DDR4_8Gb_x8_2400", working_dir=str(tmp_path), frontend_queue=False
        )
        for i in range(8):
            assert mem.submit(0x1000 + i * 64, i % 2 == 1, tag=i) is not None
        got = [(c.addr, c.tag, c.is_write) for c in mem.completions()]
        assert got == order_via_tick()

    def test_wait_alias_and_stats(self, tmp_path):
        mem = Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        mem.submit(0x1000, False)
        evs = mem.wait()
        assert len(evs) == 1
        assert evs[0].cycle == mem.current_cycle
        assert mem.wait() == []
        stats = mem.get_stats()
        assert "0" in stats

    def test_context_manager(self, tmp_path):
        with Memory.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path)) as mem:
            mem.submit(0x1000, False)
            mem.drain()
            assert not mem.busy

    def test_completion_namedtuple_compat(self):
        c = Completion(1, 2, 3, False)
        assert c.cycle == 0
        c2 = Completion(1, 2, 3, True, 10)
        assert c2.cycle == 10
