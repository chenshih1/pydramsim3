#ifndef PDRAMSIM3_SIM_ENGINE_HPP
#define PDRAMSIM3_SIM_ENGINE_HPP

#include <cstdint>
#include <memory>
#include <mutex>
#include <queue>
#include <string>
#include <unordered_map>
#include <vector>

#include "dramsim3.h"

// SimEngine owns the full DRAM hot loop:
//   - transaction submission with backpressure (try_enqueue)
//   - batched clock ticking (tick(n) / drain(max_cycles))
//   - outstanding-transaction tracking (gem5-style addr -> queue of cycles)
//   - per-transaction latency computed in C++ at completion time
//   - completion events collected in C++ buffers and exported to Python in
//     bulk (no per-event Python reentry, GIL may be released during tick)
//
// Thread safety: methods are guarded by an internal mutex, so the GIL can be
// released around long-running tick()/drain() calls.
class SimEngine {
 public:
  SimEngine(const std::string& config_file, const std::string& working_dir,
            bool collect_events);

  // Submit one transaction, optionally tagging it with an opaque request
  // id that is returned with the completion event (gem5 PacketPtr analog).
  // Returns false on backpressure (DRAMsim3's AddTransaction re-checks its
  // per-channel acceptance internally and fails when the queue is full, so
  // no separate can_accept call is needed).
  bool tryEnqueue(uint64_t addr, bool is_write, uint64_t tag);

  // Advance *cycles* clock cycles; returns the number of cycles advanced.
  // Completion events are collected internally; retrieve them with
  // takeReadEvents()/takeWriteEvents().
  uint64_t tick(uint64_t cycles);

  // Bulk trace driver: submit each (addr, writes[i]) with backpressure
  // handled internally (wait for capacity by ticking), insert gap_cycles
  // idle cycles after each transaction, then drain remaining completions
  // for up to max_drain_cycles.  Returns the total number of cycles
  // elapsed.  Pure C++ loop; Python crosses the boundary only once.
  uint64_t runTrace(const uint64_t* addrs, const bool* writes, size_t count,
                    uint64_t gap_cycles, uint64_t max_drain_cycles);

  // Tick until the next try_enqueue(addr, is_write) would succeed (global
  // outstanding below queue_size AND DRAMsim3's own per-channel acceptance
  // check passing), or max_cycles is exhausted.  Returns the number of
  // cycles executed.  Used to absorb backpressure waits inside C++ instead
  // of ping-ponging across Python.
  //
  // The per-transaction addr/is_write matters: DRAMsim3 accepts reads and
  // writes into separate per-channel queues, and write completion callbacks
  // fire one cycle after submission, so the outstanding counter alone cannot
  // tell when a specific transaction would be accepted.
  uint64_t tickUntilCapacity(uint64_t addr, bool is_write, uint64_t max_cycles);

  // Tick until no transactions are outstanding, up to max_cycles.
  // Returns the number of cycles executed (<= max_cycles).
  uint64_t drain(uint64_t max_cycles);

  // Toggle completion-event collection.  Disabling clears pending events.
  void setCollect(bool collect);

  // Retrieve and clear the collected completion events.
  // Returns (addrs, latencies, tags); latency in cycles.
  std::tuple<std::vector<uint64_t>, std::vector<uint64_t>,
             std::vector<uint64_t>>
  takeReadEvents();
  std::tuple<std::vector<uint64_t>, std::vector<uint64_t>,
             std::vector<uint64_t>>
  takeWriteEvents();

  // Outstanding transaction counters.
  uint64_t numOutstanding() const;
  uint64_t numOutstandingReads() const;
  uint64_t numOutstandingWrites() const;

  // Current cycle (absolute sim time).
  uint64_t currentCycle() const;

  // Cached configuration invariants.
  double clockPeriod() const;
  unsigned int queueSize() const;
  unsigned int burstSize() const;

  void printStats();
  void resetStats();

 private:
  void onReadComplete(uint64_t addr);
  void onWriteComplete(uint64_t addr);
  void collect(std::vector<uint64_t>* addrs, std::vector<uint64_t>* lats,
               std::vector<uint64_t>* tags, uint64_t addr,
               uint64_t submit_cycle, uint64_t tag);
  // Submits one transaction; assumes mutex_ is held.
  bool tryEnqueueLocked(uint64_t addr, bool is_write, uint64_t tag);
  // Advances one cycle; assumes mutex_ is held.
  void tickOnceLocked();

  std::unique_ptr<dramsim3::MemorySystem> dramsim_;

  // Cycle counter; incremented *after* each ClockTick so that completion
  // callbacks observe the cycle at which the completion occurs, matching
  // DRAMsim3's internal clk_ semantics.
  uint64_t cycle_ = 0;

  // gem5: std::unordered_map<Addr, std::queue<(cycle, tag)>> outstanding
  std::unordered_map<uint64_t, std::queue<std::pair<uint64_t, uint64_t>>>
      outstanding_reads_;
  std::unordered_map<uint64_t, std::queue<std::pair<uint64_t, uint64_t>>>
      outstanding_writes_;
  uint64_t num_outstanding_reads_ = 0;
  uint64_t num_outstanding_writes_ = 0;

  // Completion event buffers (addr, latency, tag) for bulk export to Python.
  bool collect_events_ = true;
  std::vector<uint64_t> read_addrs_, read_lats_, read_tags_;
  std::vector<uint64_t> write_addrs_, write_lats_, write_tags_;

  double clock_period_;
  unsigned int queue_size_;
  unsigned int burst_size_;

  mutable std::mutex mutex_;
};

#endif  // PDRAMSIM3_SIM_ENGINE_HPP
