#ifndef PDRAMSIM3_SIM_ENGINE_HPP
#define PDRAMSIM3_SIM_ENGINE_HPP

#include <cstdint>
#include <deque>
#include <memory>
#include <mutex>
#include <string>
#include <tuple>
#include <unordered_map>
#include <vector>

#include "channel_map.hpp"
#include "dramsim3.h"

// SimEngine owns the full DRAM hot loop:
//   - transaction submission with backpressure (try_enqueue)
//   - batched clock ticking (tick(n) / drain(max_cycles))
//   - outstanding-transaction tracking (addr -> queue of issue cycle + tag)
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
  // id that is returned with the completion event.
  // Returns false on backpressure (DRAMsim3's AddTransaction re-checks its
  // per-channel acceptance internally and fails when the queue is full, so
  // no separate can_accept call is needed).
  bool tryEnqueue(uint64_t addr, bool is_write, uint64_t tag);

  // Advance *cycles* clock cycles; returns the number of cycles advanced.
  // Completion events are collected internally; retrieve them with
  // takeCompletions() / takeReadCompletions() / takeWriteCompletions().
  uint64_t tick(uint64_t cycles);

  // Bulk trace driver: submit each (addr, writes[i]) with backpressure
  // handled internally (wait for capacity by ticking, fail after 10M
  // stalled cycles per transaction), insert gap_cycles idle cycles after
  // each transaction, then drain remaining completions for up to
  // max_drain_cycles.  Returns the total number of cycles elapsed.
  uint64_t runTrace(const uint64_t* addrs, const bool* writes, size_t count,
                    uint64_t gap_cycles, uint64_t max_drain_cycles);

  // Tick until the next try_enqueue(addr, is_write) would succeed
  // (DRAMsim3 WillAcceptTransaction for that address and direction), or
  // max_cycles is exhausted.  Returns the number of cycles executed.
  // Used to absorb backpressure waits inside C++ instead of ping-ponging
  // across Python.
  //
  // The per-transaction addr/is_write matters: DRAMsim3 accepts reads and
  // writes into separate per-channel queues, and write completion callbacks
  // fire one cycle after submission, so the outstanding counter alone cannot
  // tell when a specific transaction would be accepted.
  uint64_t tickUntilCapacity(uint64_t addr, bool is_write, uint64_t max_cycles);

  // Tick until at least one transaction completes (read or write callback),
  // or max_cycles is exhausted, or nothing is in flight / parked.  Returns
  // the number of cycles executed.  This is the DES-host primitive: the
  // Python event loop sleeps until the next completion instead of ticking
  // every DRAM cycle.
  uint64_t tickUntilCompletion(uint64_t max_cycles);

  // Tick until current_cycle >= target_cycle.  If stop_on_completion is
  // true, return early at the ClockTick that produces the first new
  // completion.  Returns cycles executed.
  uint64_t advanceTo(uint64_t target_cycle, bool stop_on_completion);

  // Tick until current_cycle >= target_cycle.  If stop_on_tag_done and a
  // tag quota (setTagQuota) reaches zero, return at that ClockTick so
  // the host can issue follow-up requests.  target_cycle == UINT64_MAX
  // means no deadline: stop on tag-done or idle.
  uint64_t advanceUntil(uint64_t target_cycle, bool stop_on_tag_done);

  // Remaining bursts for a host-side logical request.  Completions with
  // this tag decrement the quota; hitting zero trips stop_on_tag_done.
  void setTagQuota(uint64_t tag, uint64_t remaining);

  // Always-succeeding submit: park on a software frontend queue when the
  // controller will not accept, and drain into DRAMsim3 on later ticks.
  // Latency is measured from this enqueue's issue cycle (queue wait is
  // included).  DES hosts should use this instead of tryEnqueue.
  void enqueue(uint64_t addr, bool is_write, uint64_t tag);

  // Transactions waiting on the frontend queue (not yet in DRAMsim3).
  uint64_t frontendSize() const;

  // Tick until no transactions are outstanding, up to max_cycles.
  // Returns the number of cycles executed (<= max_cycles).  Also drains
  // the frontend queue into the controller as slots free.
  uint64_t drain(uint64_t max_cycles);

  // Toggle completion-event collection.  Disabling clears pending events.
  void setCollect(bool collect);

  // Retrieve and clear collected completions.
  // takeCompletions: all events in callback order
  //   (addrs, latencies, tags, complete_cycles, is_write).
  // takeReadCompletions / takeWriteCompletions: one direction, same fields
  //   minus is_write.
  std::tuple<std::vector<uint64_t>, std::vector<uint64_t>,
             std::vector<uint64_t>, std::vector<uint64_t>>
  takeReadCompletions();
  std::tuple<std::vector<uint64_t>, std::vector<uint64_t>,
             std::vector<uint64_t>, std::vector<uint64_t>>
  takeWriteCompletions();
  std::tuple<std::vector<uint64_t>, std::vector<uint64_t>,
             std::vector<uint64_t>, std::vector<uint64_t>,
             std::vector<uint8_t>>
  takeCompletions();

  // Outstanding transaction counters (DRAMsim3 only, excludes frontend).
  uint64_t numOutstanding() const;
  uint64_t numOutstandingReads() const;
  uint64_t numOutstandingWrites() const;

  // DRAMsim3 outstanding + frontend queue.
  uint64_t inFlight() const;

  // Current cycle (absolute sim time).
  uint64_t currentCycle() const;

  // Cached configuration invariants.
  double clockPeriod() const;
  unsigned int queueSize() const;
  unsigned int burstSize() const;

  void printStats();
  void resetStats();

 private:
  struct CompletionEvent {
    uint64_t addr;
    uint64_t latency;
    uint64_t tag;
    uint64_t cycle;
    bool is_write;
  };
  struct PendingTxn {
    uint64_t addr;
    bool is_write;
    uint64_t tag;
    uint64_t issue_cycle;
  };
  struct OutstandingTxn {
    uint64_t issue_cycle;
    uint64_t tag;
  };

  void onReadComplete(uint64_t addr);
  void onWriteComplete(uint64_t addr);
  void collect(uint64_t addr, uint64_t submit_cycle, uint64_t tag,
               bool is_write);
  std::tuple<std::vector<uint64_t>, std::vector<uint64_t>,
             std::vector<uint64_t>, std::vector<uint64_t>>
  extractCompletionsLocked(bool want_write);
  // Submits one transaction; assumes mutex_ is held.
  bool tryEnqueueLocked(uint64_t addr, bool is_write, uint64_t tag);
  bool admitLocked(uint64_t addr, bool is_write, uint64_t tag,
                   uint64_t issue_cycle);
  // Push onto the frontend and try to drain; assumes mutex_ is held.
  void enqueueLocked(uint64_t addr, bool is_write, uint64_t tag);
  // Move parked frontend transactions into DRAMsim3.  Per-(channel,
  // direction) queues give HOL bypass without scanning the full frontend
  // each cycle: a blocked write does not stall reads or other channels.
  void drainFrontendLocked();
  // Index into frontend_queues_ for (channel(addr), is_write).
  size_t frontendIndex(uint64_t addr, bool is_write) const;
  // Advances one cycle; assumes mutex_ is held.
  void tickOnceLocked();
  uint64_t inFlightLocked() const;
  void noteTagLocked(uint64_t tag);

  std::unique_ptr<dramsim3::MemorySystem> dramsim_;
  // Wrapper-owned channel extraction for frontend sharding.  Does not
  // modify or include DRAMsim3 internals; parses the same .ini the
  // MemorySystem already consumed.
  ChannelMap channel_map_;

  // Cycle counter; incremented *after* each ClockTick so that completion
  // callbacks observe the cycle at which the completion occurs, matching
  // DRAMsim3's internal clk_ semantics.
  uint64_t cycle_ = 0;

  // Per-address FIFO of in-flight transactions.
  std::unordered_map<uint64_t, std::deque<OutstandingTxn>> outstanding_reads_;
  std::unordered_map<uint64_t, std::deque<OutstandingTxn>> outstanding_writes_;
  uint64_t num_outstanding_reads_ = 0;
  uint64_t num_outstanding_writes_ = 0;

  bool collect_events_ = true;
  std::vector<CompletionEvent> events_;
  uint64_t completion_count_ = 0;

  // Host logical-request quotas: tag -> remaining bursts.
  std::unordered_map<uint64_t, uint64_t> tag_quota_;
  bool tag_done_ = false;

  // Software frontend: one FIFO per (channel, direction).  Index
  // 2*channel + is_write.  Preserves FIFO within a queue while giving
  // free queues HOL bypass over blocked ones.
  std::vector<std::deque<PendingTxn>> frontend_queues_;
  uint64_t frontend_size_ = 0;

  double clock_period_;
  unsigned int queue_size_;
  unsigned int burst_size_;

  mutable std::mutex mutex_;
};

#endif  // PDRAMSIM3_SIM_ENGINE_HPP
