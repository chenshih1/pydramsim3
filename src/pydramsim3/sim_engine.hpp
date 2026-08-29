#ifndef PYDRAMSIM3_SIM_ENGINE_HPP
#define PYDRAMSIM3_SIM_ENGINE_HPP

#include <cstdint>
#include <deque>
#include <memory>
#include <mutex>
#include <queue>
#include <string>
#include <tuple>
#include <unordered_map>
#include <vector>

#include "dramsim3.h"

// SimEngine owns the full DRAM hot loop:
//   - issue: tryAdmit / park / parkRange
//   - time: public methods only set stop predicates; advanceLocked is the
//     tick loop (Python Memory event-loop helpers pull Completions)
//   - occupancy: inFlight, frontendSize, numOutstanding*, unmatchedCallbacks
//   - completions collected in C++ and exported in bulk (no per-event
//     Python reentry; GIL may be released during tick)
//
// Declaration order is ABI-stable; do not reorder methods.  Thread safety:
// methods are guarded by an internal mutex so the GIL can be released
// around long-running tick()/drain() calls.
class SimEngine {
 public:
  SimEngine(const std::string& config_file, const std::string& working_dir,
            bool collect_events);

  // Submit one transaction, optionally tagging it with an opaque request
  // id that is returned with the completion event.
  // Returns false on backpressure (DRAMsim3's AddTransaction re-checks its
  // per-channel acceptance internally and fails when the queue is full, so
  // no separate can_accept call is needed).
  bool tryAdmit(uint64_t addr, bool is_write, uint64_t tag);

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

  // Tick until the next tryAdmit(addr, is_write) would succeed
  // (no write aliasing an in-flight read, and DRAMsim3
  // WillAcceptTransaction for that address and direction), or
  // max_cycles is exhausted.  Returns the number of cycles executed.
  // Used to absorb backpressure waits inside C++ instead of ping-ponging
  // across Python.
  //
  // The per-transaction addr/is_write matters: DRAMsim3 accepts reads and
  // writes into separate per-channel queues, and write completion callbacks
  // fire one cycle after submission, so the outstanding counter alone cannot
  // tell when a specific transaction would be accepted.
  uint64_t advanceUntilAccept(uint64_t addr, bool is_write, uint64_t max_cycles);

  // Tick until at least one transaction completes (read or write callback),
  // or max_cycles is exhausted, or nothing is in flight / parked.  Returns
  // the number of cycles executed.  This is the DES-host primitive: the
  // Python event loop sleeps until the next completion instead of ticking
  // every DRAM cycle.
  uint64_t advanceUntilCompletion(uint64_t max_cycles);

  // Tick until current_cycle >= target_cycle.  If stop_on_completion is
  // true, return early at the ClockTick that produces the first new
  // completion.  Returns cycles executed.
  uint64_t advanceTo(uint64_t target_cycle, bool stop_on_completion);

  // Tick until current_cycle >= target_cycle.  If stop_on_tag_done and a
  // tag quota (setTagQuota) reaches zero, return at that ClockTick so
  // the host can issue follow-up requests.  target_cycle == UINT64_MAX
  // means no deadline: stop on tag-done, idle, or max_cycles (0 = no
  // cap).  A finite target_cycle is not limited by max_cycles (lockstep
  // idle refresh must not stop early).
  uint64_t advanceUntil(uint64_t target_cycle, bool stop_on_tag_done,
                        uint64_t max_cycles);

  // Tick up to *cycles* from now.  Same stop_on_tag_done rules as
  // advanceUntil.  Returns cycles executed.
  uint64_t advanceBy(uint64_t cycles, bool stop_on_tag_done);

  // Remaining bursts for a host-side logical request.  Completions with
  // this tag decrement the quota; hitting zero trips stop_on_tag_done.
  void setTagQuota(uint64_t tag, uint64_t remaining);

  // Park on a software frontend queue when the controller will not
  // accept, and drain into DRAMsim3 on later ticks.  Latency is measured
  // from this park's issue cycle (queue wait is included).  DES hosts
  // should use this instead of tryAdmit.
  //
  // Returns false when outstanding_cap is set and in-flight is already
  // at the cap (nothing parked).  With cap 0 (default) always succeeds.
  bool park(uint64_t addr, bool is_write, uint64_t tag);

  // Park up to *count* consecutive bursts (addr, addr+stride, ...) then
  // drain once.  Same admission order as that many park() calls with
  // no ClockTick in between.  Returns how many were parked (less than
  // *count* when outstanding_cap is reached).
  uint64_t parkRange(uint64_t addr, uint64_t count, uint64_t stride,
                     bool is_write, uint64_t tag);

  // Finite in-flight window (DRAMsim3 outstanding + frontend).  0 means
  // unbounded parking.  Does not change per-channel HOL bypass of
  // requests already queued.
  void setOutstandingCap(uint64_t cap);
  uint64_t outstandingCap() const;
  int numChannels() const;
  uint64_t memorySize() const;
  int channelOf(uint64_t addr) const;
  bool willAccept(uint64_t addr, bool is_write) const;
  uint64_t unmatchedCallbacks() const;
  uint64_t frontendBlockedWrites() const;

  // Transactions waiting on the frontend queue (not yet in DRAMsim3).
  uint64_t frontendSize() const;

  // Tick until in-flight (controller + frontend) is below *cap*, idle,
  // or max_cycles.  Returns cycles executed.  Used by DES hosts waiting
  // for outstanding credit.
  uint64_t advanceUntilInFlightBelow(uint64_t cap, uint64_t max_cycles);

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
  struct Completion {
    uint64_t addr;
    uint64_t latency;
    uint64_t tag;
    uint64_t cycle;
    bool is_write;
  };
  struct PendingTransaction {
    uint64_t addr;
    bool is_write;
    uint64_t tag;
    uint64_t issue_cycle;
    uint64_t seq;
  };

  void onReadComplete(uint64_t addr);
  void onWriteComplete(uint64_t addr);
  void collect(uint64_t addr, uint64_t submit_cycle, uint64_t tag,
               bool is_write);
  std::tuple<std::vector<uint64_t>, std::vector<uint64_t>,
             std::vector<uint64_t>, std::vector<uint64_t>>
  extractCompletionsLocked(bool want_write);
  // Submits one transaction; assumes mutex_ is held.
  bool tryAdmitLocked(uint64_t addr, bool is_write, uint64_t tag);
  bool admitLocked(uint64_t addr, bool is_write, uint64_t tag,
                   uint64_t issue_cycle);
  bool readOutstandingLocked(uint64_t addr) const;
  // True when tryAdmit would succeed (no R→W alias, DRAMsim3 accepts).
  bool canAdmitLocked(uint64_t addr, bool is_write) const;
  // Push onto the frontend and try to drain; assumes mutex_ is held.
  // Returns false when the outstanding cap is full.
  bool parkLocked(uint64_t addr, bool is_write, uint64_t tag);
  bool atCapLocked() const;
  void pushFrontendLocked(uint64_t addr, bool is_write, uint64_t tag);
  int bucketOf(uint64_t addr, bool is_write) const;
  int channelOfLocked(uint64_t addr) const;
  // Move parked frontend transactions into DRAMsim3.  Per-channel
  // blocking does not stall later requests to a free channel.  Only
  // queue heads are tried (WillAccept is per channel and direction),
  // admitted in global park order.
  void drainFrontendLocked();
  // Advances one cycle; assumes mutex_ is held.
  void tickOnceLocked();
  uint64_t inFlightLocked() const;
  // Stop predicates for advanceLocked.  Do not use UINT64_MAX as a
  // "no target" sentinel: advanceTo(UINT64_MAX) is a real deadline.
  struct AdvanceSpec {
    uint64_t max_ticks = ~uint64_t{0};
    uint64_t target_cycle = 0;
    uint64_t in_flight_below = 0;
    uint64_t accept_addr = 0;
    bool has_target = false;
    bool accept_write = false;
    bool stop_if_idle = false;
    bool stop_on_completion = false;
    bool stop_on_tag_done = false;
    bool stop_on_accept = false;
    bool stop_on_in_flight_below = false;
    bool reset_tag_done = false;
  };
  uint64_t advanceLocked(const AdvanceSpec& spec);
  void noteTagLocked(uint64_t tag);
  uint64_t frontendBlockedWritesLocked() const;

  std::unique_ptr<dramsim3::MemorySystem> dramsim_;

  // Cycle counter; incremented *after* each ClockTick so that completion
  // callbacks observe the cycle at which the completion occurs, matching
  // DRAMsim3's internal clk_ semantics.
  uint64_t cycle_ = 0;

  // Per-address FIFO of (issue_cycle, tag) for in-flight transactions.
  std::unordered_map<uint64_t, std::queue<std::pair<uint64_t, uint64_t>>>
      outstanding_reads_;
  std::unordered_map<uint64_t, std::queue<std::pair<uint64_t, uint64_t>>>
      outstanding_writes_;
  uint64_t num_outstanding_reads_ = 0;
  uint64_t num_outstanding_writes_ = 0;
  uint64_t unmatched_callbacks_ = 0;

  bool collect_events_ = true;
  std::vector<Completion> events_;
  uint64_t completion_count_ = 0;

  // Host logical-request quotas: tag -> remaining bursts.
  std::unordered_map<uint64_t, uint64_t> tag_quota_;
  bool tag_done_ = false;

  // Software frontend: one FIFO per (channel, read/write).  WillAccept
  // is per-channel and per-direction, so a full scan of a global deque
  // is equivalent to trying these heads in park-seq order.
  std::vector<std::deque<PendingTransaction>> frontend_queues_;
  uint64_t frontend_count_ = 0;
  uint64_t park_seq_ = 0;
  int num_channels_ = 0;
  int num_buckets_ = 0;
  uint64_t memory_size_ = 0;
  // Copied from the .ini via dramsim3::Config (same formula as
  // BaseDRAMSystem::GetChannel).  Not a MemorySystem API patch.
  int shift_bits_ = 0;
  int ch_pos_ = 0;
  uint64_t ch_mask_ = 0;

  double clock_period_;
  unsigned int queue_size_;
  unsigned int burst_size_;
  uint64_t outstanding_cap_ = 0;

  mutable std::mutex mutex_;
};

#endif  // PYDRAMSIM3_SIM_ENGINE_HPP
