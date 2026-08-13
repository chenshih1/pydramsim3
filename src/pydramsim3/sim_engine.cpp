#include "sim_engine.hpp"

#include <stdexcept>
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

bool SimEngine::tryEnqueue(uint64_t addr, bool is_write) {
  std::lock_guard<std::mutex> lock(mutex_);
  return tryEnqueueLocked(addr, is_write);
}

bool SimEngine::tryEnqueueLocked(uint64_t addr, bool is_write) {
  // Global outstanding cap (gem5 parity): DRAMsim3's acceptance check is
  // per-channel, so without this cap multi-channel traces could exceed
  // queue_size outstanding transactions.
  if (nbr_outstanding_reads_ + nbr_outstanding_writes_ >= queue_size_) {
    return false;
  }
  if (!dramsim_->AddTransaction(addr, is_write)) {
    return false;
  }
  if (is_write) {
    outstanding_writes_[addr].push(cycle_);
    ++nbr_outstanding_writes_;
  } else {
    outstanding_reads_[addr].push(cycle_);
    ++nbr_outstanding_reads_;
  }
  return true;
}

uint64_t SimEngine::tick(uint64_t cycles) {
  std::lock_guard<std::mutex> lock(mutex_);
  for (uint64_t i = 0; i < cycles; ++i) {
    tickOnceLocked();
  }
  return cycles;
}

void SimEngine::tickOnceLocked() {
  dramsim_->ClockTick();
  ++cycle_;
}

uint64_t SimEngine::runTrace(const uint64_t* addrs, const bool* writes,
                             size_t count, uint64_t gap_cycles,
                             uint64_t max_drain_cycles) {
  std::lock_guard<std::mutex> lock(mutex_);
  const uint64_t start = cycle_;
  for (size_t i = 0; i < count; ++i) {
    const uint64_t addr = addrs[i];
    const bool is_write = writes[i] != 0;
    while (!tryEnqueueLocked(addr, is_write)) {
      // Backpressure: tick until DRAMsim3 accepts this exact transaction.
      tickOnceLocked();
    }
    for (uint64_t g = 0; g < gap_cycles; ++g) {
      tickOnceLocked();
    }
  }
  uint64_t n = 0;
  while (n < max_drain_cycles &&
         (nbr_outstanding_reads_ + nbr_outstanding_writes_) > 0) {
    tickOnceLocked();
    ++n;
  }
  return cycle_ - start;
}

uint64_t SimEngine::tickUntilCapacity(uint64_t addr, bool is_write,
                                      uint64_t max_cycles) {
  std::lock_guard<std::mutex> lock(mutex_);
  uint64_t n = 0;
  while (n < max_cycles &&
         ((nbr_outstanding_reads_ + nbr_outstanding_writes_) >= queue_size_ ||
          !dramsim_->WillAcceptTransaction(addr, is_write))) {
    dramsim_->ClockTick();
    ++cycle_;
    ++n;
  }
  return n;
}

uint64_t SimEngine::drain(uint64_t max_cycles) {
  std::lock_guard<std::mutex> lock(mutex_);
  uint64_t n = 0;
  while (n < max_cycles &&
         (nbr_outstanding_reads_ + nbr_outstanding_writes_) > 0) {
    tickOnceLocked();
    ++n;
  }
  return n;
}

void SimEngine::setCollect(bool collect) {
  std::lock_guard<std::mutex> lock(mutex_);
  collect_events_ = collect;
  if (!collect) {
    read_addrs_.clear();
    read_lats_.clear();
    write_addrs_.clear();
    write_lats_.clear();
  }
}

std::pair<std::vector<uint64_t>, std::vector<uint64_t>>
SimEngine::takeReadEvents() {
  std::lock_guard<std::mutex> lock(mutex_);
  std::pair<std::vector<uint64_t>, std::vector<uint64_t>> out;
  out.first.swap(read_addrs_);
  out.second.swap(read_lats_);
  return out;
}

std::pair<std::vector<uint64_t>, std::vector<uint64_t>>
SimEngine::takeWriteEvents() {
  std::lock_guard<std::mutex> lock(mutex_);
  std::pair<std::vector<uint64_t>, std::vector<uint64_t>> out;
  out.first.swap(write_addrs_);
  out.second.swap(write_lats_);
  return out;
}

uint64_t SimEngine::numOutstanding() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return nbr_outstanding_reads_ + nbr_outstanding_writes_;
}

uint64_t SimEngine::numOutstandingReads() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return nbr_outstanding_reads_;
}

uint64_t SimEngine::numOutstandingWrites() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return nbr_outstanding_writes_;
}

uint64_t SimEngine::cycle() const {
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
    uint64_t submit_cycle = it->second.front();
    it->second.pop();
    if (it->second.empty()) {
      outstanding_reads_.erase(it);
    }
    --nbr_outstanding_reads_;
    collect(&read_addrs_, &read_lats_, addr, submit_cycle);
  }
}

void SimEngine::onWriteComplete(uint64_t addr) {
  auto it = outstanding_writes_.find(addr);
  if (it != outstanding_writes_.end()) {
    uint64_t submit_cycle = it->second.front();
    it->second.pop();
    if (it->second.empty()) {
      outstanding_writes_.erase(it);
    }
    --nbr_outstanding_writes_;
    collect(&write_addrs_, &write_lats_, addr, submit_cycle);
  }
}

void SimEngine::collect(std::vector<uint64_t>* addrs,
                        std::vector<uint64_t>* lats, uint64_t addr,
                        uint64_t submit_cycle) {
  if (!collect_events_) {
    return;
  }
  addrs->push_back(addr);
  lats->push_back(cycle_ - submit_cycle);
}
