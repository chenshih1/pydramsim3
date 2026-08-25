#include "sim_engine.hpp"

#include <stdexcept>
#include <tuple>
#include <utility>

SimEngine::SimEngine(const std::string& config_file,
                     const std::string& working_dir, bool collect_events)
    : dramsim_(dramsim3::GetMemorySystem(
          config_file, working_dir,
          [this](uint64_t addr) { onReadComplete(addr); },
          [this](uint64_t addr) { onWriteComplete(addr); })),
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
  if (is_write) {
    outstanding_writes_[addr].push(std::make_pair(issue_cycle, tag));
    ++num_outstanding_writes_;
  } else {
    outstanding_reads_[addr].push(std::make_pair(issue_cycle, tag));
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
  drainFrontendLocked();
  dramsim_->ClockTick();
  ++cycle_;
  drainFrontendLocked();
}

void SimEngine::enqueueLocked(uint64_t addr, bool is_write, uint64_t tag) {
  frontend_.push_back(PendingTxn{addr, is_write, tag, cycle_});
  drainFrontendLocked();
}

void SimEngine::enqueue(uint64_t addr, bool is_write, uint64_t tag) {
  std::lock_guard<std::mutex> lock(mutex_);
  enqueueLocked(addr, is_write, tag);
}

void SimEngine::drainFrontendLocked() {
  for (auto it = frontend_.begin(); it != frontend_.end();) {
    if (!dramsim_->WillAcceptTransaction(it->addr, it->is_write) ||
        !admitLocked(it->addr, it->is_write, it->tag, it->issue_cycle)) {
      ++it;
      continue;
    }
    it = frontend_.erase(it);
  }
}

uint64_t SimEngine::inFlightLocked() const {
  return num_outstanding_reads_ + num_outstanding_writes_ +
         static_cast<uint64_t>(frontend_.size());
}

uint64_t SimEngine::runTrace(const uint64_t* addrs, const bool* writes,
                             size_t count, uint64_t gap_cycles,
                             uint64_t max_drain_cycles) {
  std::lock_guard<std::mutex> lock(mutex_);
  const uint64_t start = cycle_;
  for (size_t i = 0; i < count; ++i) {
    const uint64_t addr = addrs[i];
    const bool is_write = writes[i] != 0;
    uint64_t stall = 0;
    while (!tryEnqueueLocked(addr, is_write, 0)) {
      // Backpressure: tick until DRAMsim3 accepts this exact transaction.
      if (stall >= 10000000ULL) {
        throw std::runtime_error(
            "run_trace: backpressure not cleared after 10000000 cycles");
      }
      tickOnceLocked();
      ++stall;
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

uint64_t SimEngine::frontendSize() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return static_cast<uint64_t>(frontend_.size());
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
  for (const auto& ev : events_) {
    if (ev.is_write == want_write) {
      std::get<0>(out).push_back(ev.addr);
      std::get<1>(out).push_back(ev.latency);
      std::get<2>(out).push_back(ev.tag);
      std::get<3>(out).push_back(ev.cycle);
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
    uint64_t submit_cycle = it->second.front().first;
    uint64_t tag = it->second.front().second;
    it->second.pop();
    if (it->second.empty()) {
      outstanding_reads_.erase(it);
    }
    --num_outstanding_reads_;
    ++completion_count_;
    collect(addr, submit_cycle, tag, false);
  }
}

void SimEngine::onWriteComplete(uint64_t addr) {
  auto it = outstanding_writes_.find(addr);
  if (it != outstanding_writes_.end()) {
    uint64_t submit_cycle = it->second.front().first;
    uint64_t tag = it->second.front().second;
    it->second.pop();
    if (it->second.empty()) {
      outstanding_writes_.erase(it);
    }
    --num_outstanding_writes_;
    ++completion_count_;
    collect(addr, submit_cycle, tag, true);
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
