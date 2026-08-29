#include "sim_engine.hpp"

#include <cstdint>
#include <limits>
#include <stdexcept>
#include <tuple>
#include <utility>

SimEngine::SimEngine(const std::string& config_file,
                     const std::string& working_dir, bool collect_events)
    : dramsim_(dramsim3::GetMemorySystem(
          config_file, working_dir,
          [this](uint64_t addr) { onReadComplete(addr); },
          [this](uint64_t addr) { onWriteComplete(addr); })),
      addr_cfg_(std::make_unique<dramsim3::Config>(config_file, working_dir)),
      collect_events_(collect_events),
      clock_period_(0.0),
      queue_size_(0),
      burst_size_(0) {
  if (!dramsim_) {
    throw std::runtime_error("Failed to create DRAMsim3 MemorySystem");
  }
  double tck = dramsim_->GetTCK();
  if (tck == 0.0) {
    throw std::runtime_error("Failed to read DRAM clock period (tCK)");
  }
  clock_period_ = tck;

  int qs = dramsim_->GetQueueSize();
  if (qs <= 0) {
    throw std::runtime_error("Failed to read DRAM transaction queue size");
  }
  queue_size_ = static_cast<unsigned int>(qs);

  int bus = dramsim_->GetBusBits();
  int burst = dramsim_->GetBurstLength();
  if (bus <= 0 || burst <= 0) {
    throw std::runtime_error("Failed to read DRAM burst parameters");
  }
  burst_size_ = static_cast<unsigned int>(bus) * static_cast<unsigned int>(burst) / 8;

  if (addr_cfg_->channels <= 0) {
    throw std::runtime_error("Failed to read DRAM channel count");
  }
  // Two FIFOs per channel (read + write), matching DRAMsim3's separate
  // per-channel read/write admission.
  frontend_queues_.assign(static_cast<size_t>(addr_cfg_->channels) * 2,
                          std::deque<PendingTxn>{});
}

size_t SimEngine::frontendIndex(uint64_t addr, bool is_write) const {
  const int channel = addr_cfg_->AddressMapping(addr).channel;
  return static_cast<size_t>(channel) * 2u + (is_write ? 1u : 0u);
}

bool SimEngine::tryEnqueue(uint64_t addr, bool is_write, uint64_t tag) {
  std::lock_guard<std::mutex> lock(mutex_);
  return tryEnqueueLocked(addr, is_write, tag);
}

bool SimEngine::admitLocked(uint64_t addr, bool is_write, uint64_t tag,
                            uint64_t issue_cycle) {
  if (!dramsim_->AddTransaction(addr, is_write)) {
    return false;
  }
  OutstandingTxn ot{issue_cycle, tag};
  if (is_write) {
    outstanding_writes_[addr].push_back(ot);
    ++num_outstanding_writes_;
  } else {
    outstanding_reads_[addr].push_back(ot);
    ++num_outstanding_reads_;
  }
  return true;
}

bool SimEngine::tryEnqueueLocked(uint64_t addr, bool is_write, uint64_t tag) {
  return admitLocked(addr, is_write, tag, cycle_);
}

uint64_t SimEngine::tick(uint64_t cycles) {
  std::lock_guard<std::mutex> lock(mutex_);
  for (uint64_t i = 0; i < cycles; ++i) {
    tickOnceLocked();
  }
  return cycles;
}

void SimEngine::tickOnceLocked() {
  // enqueue() already drains the frontend, and the previous tick's
  // post-ClockTick drain left queues as full as they can be.  Only drain
  // after ClockTick, when completions may free controller slots.
  dramsim_->ClockTick();
  ++cycle_;
  if (frontend_size_ != 0) {
    drainFrontendLocked();
  }
}

void SimEngine::enqueueLocked(uint64_t addr, bool is_write, uint64_t tag) {
  frontend_queues_[frontendIndex(addr, is_write)].push_back(
      PendingTxn{addr, is_write, tag, cycle_});
  ++frontend_size_;
  drainFrontendLocked();
}

void SimEngine::enqueue(uint64_t addr, bool is_write, uint64_t tag) {
  std::lock_guard<std::mutex> lock(mutex_);
  enqueueLocked(addr, is_write, tag);
}

void SimEngine::drainFrontendLocked() {
  if (frontend_size_ == 0) {
    return;
  }
  for (auto& q : frontend_queues_) {
    while (!q.empty()) {
      const PendingTxn& txn = q.front();
      // WillAccept first: Jedec AddTransaction updates last_req_clk_ even
      // on a rejected submit, which would skew interarrival stats.
      if (!dramsim_->WillAcceptTransaction(txn.addr, txn.is_write) ||
          !admitLocked(txn.addr, txn.is_write, txn.tag, txn.issue_cycle)) {
        break;
      }
      q.pop_front();
      --frontend_size_;
    }
  }
}

uint64_t SimEngine::inFlightLocked() const {
  return num_outstanding_reads_ + num_outstanding_writes_ + frontend_size_;
}

uint64_t SimEngine::runTrace(const uint64_t* addrs, const bool* writes,
                             size_t count, uint64_t gap_cycles,
                             uint64_t max_drain_cycles) {
  std::lock_guard<std::mutex> lock(mutex_);
  const uint64_t start = cycle_;
  if (collect_events_) {
    events_.reserve(events_.size() + count);
  }
  for (size_t i = 0; i < count; ++i) {
    const uint64_t addr = addrs[i];
    const bool is_write = writes[i] != 0;
    uint64_t stall = 0;
    // Wait on WillAccept so a full queue does not touch last_req_clk_.
    while (!dramsim_->WillAcceptTransaction(addr, is_write)) {
      if (stall >= 10000000ULL) {
        throw std::runtime_error(
            "run_trace: backpressure not cleared after 10000000 cycles");
      }
      tickOnceLocked();
      ++stall;
    }
    if (!tryEnqueueLocked(addr, is_write, 0)) {
      throw std::runtime_error(
          "run_trace: WillAccept true but AddTransaction failed");
    }
    for (uint64_t g = 0; g < gap_cycles; ++g) {
      tickOnceLocked();
    }
  }
  uint64_t n = 0;
  while (n < max_drain_cycles && inFlightLocked() > 0) {
    tickOnceLocked();
    ++n;
  }
  return cycle_ - start;
}

uint64_t SimEngine::tickUntilCapacity(uint64_t addr, bool is_write,
                                      uint64_t max_cycles) {
  std::lock_guard<std::mutex> lock(mutex_);
  uint64_t n = 0;
  while (n < max_cycles && !dramsim_->WillAcceptTransaction(addr, is_write)) {
    tickOnceLocked();
    ++n;
  }
  return n;
}

uint64_t SimEngine::tickUntilCompletion(uint64_t max_cycles) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (inFlightLocked() == 0) {
    return 0;
  }
  const uint64_t mark = completion_count_;
  uint64_t n = 0;
  while (n < max_cycles && inFlightLocked() > 0 &&
         completion_count_ == mark) {
    tickOnceLocked();
    ++n;
  }
  return n;
}

uint64_t SimEngine::advanceTo(uint64_t target_cycle, bool stop_on_completion) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (target_cycle <= cycle_) {
    return 0;
  }
  const uint64_t mark = completion_count_;
  uint64_t n = 0;
  while (cycle_ < target_cycle) {
    tickOnceLocked();
    ++n;
    if (stop_on_completion && completion_count_ > mark) {
      break;
    }
  }
  return n;
}

void SimEngine::setTagQuota(uint64_t tag, uint64_t remaining) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (remaining == 0) {
    tag_quota_.erase(tag);
    return;
  }
  tag_quota_[tag] = remaining;
}

void SimEngine::noteTagLocked(uint64_t tag) {
  auto it = tag_quota_.find(tag);
  if (it == tag_quota_.end()) {
    return;
  }
  if (it->second > 0) {
    --it->second;
  }
  if (it->second == 0) {
    tag_done_ = true;
    tag_quota_.erase(it);
  }
}

uint64_t SimEngine::advanceUntil(uint64_t target_cycle, bool stop_on_tag_done) {
  std::lock_guard<std::mutex> lock(mutex_);
  tag_done_ = false;
  uint64_t n = 0;
  const bool bounded = target_cycle != std::numeric_limits<uint64_t>::max();
  while (true) {
    if (bounded && cycle_ >= target_cycle) {
      break;
    }
    if (!bounded && inFlightLocked() == 0) {
      break;
    }
    if (stop_on_tag_done && tag_done_) {
      break;
    }
    tickOnceLocked();
    ++n;
    if (stop_on_tag_done && tag_done_) {
      break;
    }
  }
  return n;
}

uint64_t SimEngine::frontendSize() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return frontend_size_;
}

uint64_t SimEngine::inFlight() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return inFlightLocked();
}

uint64_t SimEngine::drain(uint64_t max_cycles) {
  std::lock_guard<std::mutex> lock(mutex_);
  uint64_t n = 0;
  while (n < max_cycles && inFlightLocked() > 0) {
    tickOnceLocked();
    ++n;
  }
  return n;
}

void SimEngine::setCollect(bool collect) {
  std::lock_guard<std::mutex> lock(mutex_);
  collect_events_ = collect;
  if (!collect) {
    events_.clear();
  }
}

std::tuple<std::vector<uint64_t>, std::vector<uint64_t>,
           std::vector<uint64_t>, std::vector<uint64_t>>
SimEngine::extractCompletionsLocked(bool want_write) {
  std::tuple<std::vector<uint64_t>, std::vector<uint64_t>,
             std::vector<uint64_t>, std::vector<uint64_t>>
      out;
  std::vector<CompletionEvent> keep;
  keep.reserve(events_.size());
  auto& addrs = std::get<0>(out);
  auto& lats = std::get<1>(out);
  auto& tags = std::get<2>(out);
  auto& cycles = std::get<3>(out);
  addrs.reserve(events_.size());
  lats.reserve(events_.size());
  tags.reserve(events_.size());
  cycles.reserve(events_.size());
  for (const auto& ev : events_) {
    if (ev.is_write == want_write) {
      addrs.push_back(ev.addr);
      lats.push_back(ev.latency);
      tags.push_back(ev.tag);
      cycles.push_back(ev.cycle);
    } else {
      keep.push_back(ev);
    }
  }
  events_.swap(keep);
  return out;
}

std::tuple<std::vector<uint64_t>, std::vector<uint64_t>,
           std::vector<uint64_t>, std::vector<uint64_t>>
SimEngine::takeReadCompletions() {
  std::lock_guard<std::mutex> lock(mutex_);
  return extractCompletionsLocked(false);
}

std::tuple<std::vector<uint64_t>, std::vector<uint64_t>,
           std::vector<uint64_t>, std::vector<uint64_t>>
SimEngine::takeWriteCompletions() {
  std::lock_guard<std::mutex> lock(mutex_);
  return extractCompletionsLocked(true);
}

std::tuple<std::vector<uint64_t>, std::vector<uint64_t>,
           std::vector<uint64_t>, std::vector<uint64_t>,
           std::vector<uint8_t>>
SimEngine::takeCompletions() {
  std::lock_guard<std::mutex> lock(mutex_);
  std::tuple<std::vector<uint64_t>, std::vector<uint64_t>,
             std::vector<uint64_t>, std::vector<uint64_t>,
             std::vector<uint8_t>>
      out;
  auto& addrs = std::get<0>(out);
  auto& lats = std::get<1>(out);
  auto& tags = std::get<2>(out);
  auto& cycles = std::get<3>(out);
  auto& writes = std::get<4>(out);
  addrs.reserve(events_.size());
  lats.reserve(events_.size());
  tags.reserve(events_.size());
  cycles.reserve(events_.size());
  writes.reserve(events_.size());
  for (const auto& ev : events_) {
    addrs.push_back(ev.addr);
    lats.push_back(ev.latency);
    tags.push_back(ev.tag);
    cycles.push_back(ev.cycle);
    writes.push_back(ev.is_write ? 1 : 0);
  }
  events_.clear();
  return out;
}

uint64_t SimEngine::numOutstanding() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return num_outstanding_reads_ + num_outstanding_writes_;
}

uint64_t SimEngine::numOutstandingReads() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return num_outstanding_reads_;
}

uint64_t SimEngine::numOutstandingWrites() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return num_outstanding_writes_;
}

uint64_t SimEngine::currentCycle() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return cycle_;
}

double SimEngine::clockPeriod() const { return clock_period_; }

unsigned int SimEngine::queueSize() const { return queue_size_; }

unsigned int SimEngine::burstSize() const { return burst_size_; }

void SimEngine::printStats() {
  std::lock_guard<std::mutex> lock(mutex_);
  dramsim_->PrintStats();
}

void SimEngine::resetStats() {
  std::lock_guard<std::mutex> lock(mutex_);
  dramsim_->ResetStats();
}

void SimEngine::onReadComplete(uint64_t addr) {
  auto it = outstanding_reads_.find(addr);
  if (it != outstanding_reads_.end()) {
    const OutstandingTxn ot = it->second.front();
    it->second.pop_front();
    if (it->second.empty()) {
      outstanding_reads_.erase(it);
    }
    --num_outstanding_reads_;
    ++completion_count_;
    noteTagLocked(ot.tag);
    collect(addr, ot.issue_cycle, ot.tag, false);
  }
}

void SimEngine::onWriteComplete(uint64_t addr) {
  auto it = outstanding_writes_.find(addr);
  if (it != outstanding_writes_.end()) {
    const OutstandingTxn ot = it->second.front();
    it->second.pop_front();
    if (it->second.empty()) {
      outstanding_writes_.erase(it);
    }
    --num_outstanding_writes_;
    ++completion_count_;
    noteTagLocked(ot.tag);
    collect(addr, ot.issue_cycle, ot.tag, true);
  }
}

void SimEngine::collect(uint64_t addr, uint64_t submit_cycle, uint64_t tag,
                        bool is_write) {
  if (!collect_events_) {
    return;
  }
  // Callbacks fire during ClockTick, before tickOnceLocked increments
  // cycle_.  The DES-visible timestamp is the engine current_cycle after
  // that increment (matching Memory.current_cycle after wait()).
  events_.push_back(CompletionEvent{addr, cycle_ - submit_cycle, tag,
                                    cycle_ + 1, is_write});
}
