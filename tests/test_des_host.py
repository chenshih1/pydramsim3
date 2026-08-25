"""DES-facing Memory API: submit / wait / pull."""

import pytest

from pydramsim3 import Completion, Memory, RequestType, configs_dir
from pydramsim3._dramsim3 import SimEngine


def _engine(tmp_path, collect=True):
    cfg = str(configs_dir() / "DDR4_8Gb_x8_2400.ini")
    return SimEngine(cfg, str(tmp_path), collect)


class TestTickUntilCompletion:
    def test_stops_at_first_completion(self, tmp_path):
        e = _engine(tmp_path)
        assert e.try_enqueue(0x1000, False, tag=1)
        n = e.tick_until_completion()
        assert n > 0
        assert e.current_cycle == n
        addrs, lats, tags, cycles = e.take_read_completions()
        assert addrs == [0x1000]
        assert tags == [1]
        assert lats[0] > 0
        assert cycles == [n]
        # One-by-one tick must match the DES timestamp.
        e2 = _engine(tmp_path)
        e2.try_enqueue(0x1000, False, tag=1)
        for _ in range(n):
            e2.tick(1)
        assert e2.current_cycle == n
        _, lats2, _, cycles2 = e2.take_read_completions()
        assert lats2 == lats
        assert cycles2 == cycles

    def test_idle_returns_zero(self, tmp_path):
        e = _engine(tmp_path)
        assert e.tick_until_completion() == 0

    def test_write_completion(self, tmp_path):
        e = _engine(tmp_path)
        e.try_enqueue(0x2000, True, tag=7)
        n = e.tick_until_completion()
        assert n > 0
        addrs, _, tags, cycles = e.take_write_completions()
        assert addrs == [0x2000]
        assert tags == [7]
        assert cycles == [n]

    def test_advance_to_deadline(self, tmp_path):
        e = _engine(tmp_path)
        e.try_enqueue(0x1000, False)
        n = e.advance_to(5, stop_on_completion=False)
        assert n == 5
        assert e.current_cycle == 5
        # Read has not completed in 5 cycles on DDR4-2400.
        addrs, _, _, _ = e.take_read_completions()
        assert addrs == []

    def test_advance_to_stops_on_completion(self, tmp_path):
        e = _engine(tmp_path)
        e.try_enqueue(0x1000, False)
        n = e.advance_to(10_000, stop_on_completion=True)
        assert 0 < n < 10_000
        addrs, _, _, cycles = e.take_read_completions()
        assert len(addrs) == 1
        assert cycles == [n]


class TestFrontendQueue:
    def test_enqueue_beyond_queue_size(self, tmp_path):
        e = _engine(tmp_path)
        n_submit = e.queue_size + 16
        for i in range(n_submit):
            e.enqueue(0x1000 + i * 64, False, tag=i + 1)
        assert e.frontend_size() > 0
        # Single-channel DDR4: the read queue fills at trans_queue_size.
        assert e.num_outstanding() == e.queue_size
        e.drain()
        addrs, _, tags, _ = e.take_read_completions()
        assert len(addrs) == n_submit
        assert tags == list(range(1, n_submit + 1))
        assert e.frontend_size() == 0
        assert e.num_outstanding() == 0

    def test_latency_includes_queue_wait(self, tmp_path):
        e = _engine(tmp_path)
        # Fill the controller, then park one extra.
        for i in range(e.queue_size):
            e.enqueue(0x1000 + i * 64, False, tag=i)
        e.enqueue(0x9000, False, tag=99)
        assert e.frontend_size() >= 1
        e.drain()
        _, lats, tags, _ = e.take_read_completions()
        parked = lats[tags.index(99)]
        first = lats[tags.index(0)]
        assert parked >= first

    def test_in_flight_includes_frontend(self, tmp_path):
        e = _engine(tmp_path)
        n_submit = e.queue_size + 8
        for i in range(n_submit):
            e.enqueue(0x1000 + i * 64, False, tag=i)
        assert e.in_flight() == n_submit
        assert e.in_flight() == e.num_outstanding() + e.frontend_size()
        e.drain()
        assert e.in_flight() == 0

    def test_hol_read_bypasses_blocked_writes(self, tmp_path):
        """A blocked write at the frontend head must not stall a later read.

        Write callbacks fire one cycle after AddTransaction while the write
        buffer can stay full; HOL bypass admits the read into the free
        read queue.
        """
        e = _engine(tmp_path)
        n = e.queue_size + 8
        for i in range(n):
            e.enqueue(0x1000 + i * 64, True, tag=i)
        e.enqueue(0x9000, False, tag=999)
        assert e.frontend_size() > 0
        e.tick(2)
        assert e.num_outstanding_reads() == 1
        assert e.frontend_size() > 0


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
        with pytest.raises(RuntimeError, match="wait"):
            mem.wait(max_cycles=1)

    def test_mixed_completion_order_matches_tick_one(self, tmp_path):
        def order_via_tick():
            e = _engine(tmp_path)
            for i in range(8):
                assert e.try_enqueue(0x1000 + i * 64, i % 2 == 1, tag=i)
            seen = []
            while e.in_flight() > 0:
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
