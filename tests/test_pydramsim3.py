import json
import tempfile
from pathlib import Path

import pytest

import pydramsim3
from pydramsim3 import MemorySystem, configs_dir, list_configs


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
# Construction
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_from_config(self, tmp_path):
        mem = MemorySystem.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        assert mem.clock_period > 0
        assert mem.queue_size > 0
        assert mem.burst_size > 0

    def test_from_config_with_ini_suffix(self, tmp_path):
        mem = MemorySystem.from_config("DDR4_8Gb_x8_2400.ini", working_dir=str(tmp_path))
        assert mem.clock_period > 0

    def test_from_config_invalid(self):
        with pytest.raises(FileNotFoundError, match="not found"):
            MemorySystem.from_config("NONEXISTENT_CONFIG")

    def test_direct_path(self, tmp_path):
        cfg = configs_dir() / "DDR4_8Gb_x8_2400.ini"
        mem = MemorySystem(str(cfg), working_dir=str(tmp_path))
        assert mem.clock_period > 0

    def test_none_callbacks(self, tmp_path):
        mem = MemorySystem.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        assert mem.can_accept(0x1000, False)
        mem.enqueue(0x1000, False)
        mem.tick()

    def test_repr(self, tmp_path):
        mem = MemorySystem.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        r = repr(mem)
        assert "DDR4_8Gb_x8_2400.ini" in r
        assert "tck=" in r


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

class TestProperties:
    @pytest.fixture
    def mem(self, tmp_path):
        return MemorySystem.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))

    def test_clock_period(self, mem):
        assert 0.5 < mem.clock_period < 2.0  # DDR4-2400 tCK ≈ 0.83ns

    def test_queue_size(self, mem):
        assert mem.queue_size == 32

    def test_burst_size(self, mem):
        assert mem.burst_size == 64  # 8 bytes * 8 burst length


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

class TestSimulation:
    @pytest.fixture
    def mem(self, tmp_path):
        return MemorySystem.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))

    def test_can_accept_and_enqueue(self, mem):
        assert mem.can_accept(0x1000, False)
        mem.enqueue(0x1000, False)

    def test_tick(self, mem):
        mem.tick()

    def test_run(self, mem):
        result = mem.run(100)
        assert result == 100

    def test_read_completion(self, mem):
        completed = []
        mem.set_callbacks(read_complete=lambda addr: completed.append(addr))
        addr = 0x1000
        mem.enqueue(addr, False)
        for _ in range(2000):
            mem.tick()
        assert addr in completed

    def test_write_completion(self, mem):
        completed = []
        mem.set_callbacks(write_complete=lambda addr: completed.append(addr))
        addr = 0x2000
        mem.enqueue(addr, True)
        for _ in range(2000):
            mem.tick()
        assert addr in completed

    def test_multiple_transactions(self, mem):
        reads = []
        mem.set_callbacks(read_complete=lambda a: reads.append(a))
        addrs = [0x1000 + i * 64 for i in range(10)]
        for a in addrs:
            if mem.can_accept(a, False):
                mem.enqueue(a, False)
        mem.run(2000)
        assert len(reads) == 10

    def test_set_callbacks_replace(self, mem):
        first = []
        second = []
        mem.set_callbacks(read_complete=lambda a: first.append(a))
        mem.enqueue(0x1000, False)
        mem.run(2000)
        assert len(first) == 1
        assert len(second) == 0

        mem.set_callbacks(read_complete=lambda a: second.append(a))
        mem.enqueue(0x2000, False)
        mem.run(2000)
        assert len(first) == 1  # not called again
        assert len(second) == 1

    def test_reset_stats(self, mem):
        mem.enqueue(0x1000, False)
        mem.run(2000)
        mem.reset_stats()
        # After reset, stats should reflect zero activity
        stats = mem.get_stats()
        assert stats["0"]["num_reads_done"] == 0


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------

class TestContextManager:
    def test_with_statement(self, tmp_path):
        with MemorySystem.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path)) as mem:
            mem.run(10)
            assert mem.clock_period > 0


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

class TestStats:
    @pytest.fixture
    def mem(self, tmp_path):
        m = MemorySystem.from_config("DDR4_8Gb_x8_2400", working_dir=str(tmp_path))
        # Generate some traffic
        for i in range(16):
            addr = 0x1000 + i * 64
            if m.can_accept(addr, False):
                m.enqueue(addr, False)
        m.run(2000)
        return m

    def test_print_stats_creates_files(self, mem):
        mem.print_stats()
        assert mem.stats_json_path.exists()
        assert mem.stats_txt_path.exists()

    def test_get_stats_returns_dict(self, mem):
        stats = mem.get_stats()
        assert isinstance(stats, dict)
        assert "0" in stats

    def test_get_stats_has_expected_fields(self, mem):
        ch0 = mem.get_stats()["0"]
        assert "num_reads_done" in ch0
        assert "average_read_latency" in ch0
        assert "total_energy" in ch0
        assert "read_latency" in ch0
        assert "num_writes_done" in ch0

    def test_get_stats_reads_match(self, mem):
        ch0 = mem.get_stats()["0"]
        assert ch0["num_reads_done"] == 16

    def test_stats_json_path_property(self, mem):
        assert str(mem.stats_json_path).endswith("dramsim3.json")

    def test_stats_txt_path_property(self, mem):
        assert str(mem.stats_txt_path).endswith("dramsim3.txt")


# ---------------------------------------------------------------------------
# Different configs
# ---------------------------------------------------------------------------

class TestDifferentConfigs:
    @pytest.mark.parametrize("config_name", [
        "DDR3_4Gb_x8_1600",
        "HBM2_8Gb_x128",
        "LPDDR4_8Gb_x16_2400",
    ])
    def test_config_loads(self, config_name, tmp_path):
        mem = MemorySystem.from_config(config_name, working_dir=str(tmp_path))
        assert mem.clock_period > 0
        assert mem.burst_size > 0
        mem.run(10)
