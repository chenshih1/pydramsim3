#include "sim_engine.hpp"

#include "configuration.h"

#include <cstddef>
#include <cstdint>
#include <iterator>
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
      collect_events_(collect_events),
      clock_period_(0.0),
      queue_size_(0),
      burst_size_(0) {
  if (!dramsim_) {
    throw std::runtime_error("Failed to create DRAMsim3 MemorySystem");
  }
  double clock_ns = dramsim_->GetTCK();
  if (clock_ns == 0.0) {
    throw std::runtime_error("Failed to read DRAM clock period (tCK)");
  }
  clock_period_ = clock_ns;

  int trans_queue_size = dramsim_->GetQueueSize();
  if (trans_queue_size <= 0) {
    throw std::runtime_error("Failed to read DRAM transaction queue size");
  }
  queue_size_ = static_cast<unsigned int>(trans_queue_size);

  int bus_bits = dramsim_->GetBusBits();
  int burst_length = dramsim_->GetBurstLength();
  if (bus_bits <= 0 || burst_length <= 0) {
    throw std::runtime_error("Failed to read DRAM burst parameters");
  }
  burst_size_ = static_cast<unsigned int>(bus_bits) *
                static_cast<unsigned int>(burst_length) / 8;

  // Channel map from the same .ini DRAMsim3 already parsed.  A second
  // Config only reads mapping fields; it does not tick or write stats.
  {
    dramsim3::Config cfg(config_file, working_dir);
    num_channels_ = cfg.channels;
    shift_bits_ = cfg.shift_bits;
    ch_pos_ = cfg.ch_pos;
    ch_mask_ = cfg.ch_mask;
    // channel_size is MB after Config::CalculateSize.
    if (cfg.channel_size < 0 || cfg.channels < 0) {
      throw std::runtime_error("Failed to read DRAM address-space size");
    }
    memory_size_ = static_cast<uint64_t>(cfg.channels) *
                   static_cast<uint64_t>(cfg.channel_size) * (1ULL << 20);
  }
  if (num_channels_ <= 0) {
    throw std::runtime_error("Failed to read DRAM channel count");
  }
  num_buckets_ = num_channels_ * 2;
  frontend_queues_.assign(static_cast<size_t>(num_buckets_), {});
}

bool SimEngine::tryAdmit(uint64_t addr, bool is_write, uint64_t tag) {
  std::lock_guard<std::mutex> lock(mutex_);
  return tryAdmitLocked(addr, is_write, tag);
}

bool SimEngine::readOutstandingLocked(uint64_t addr) const {
  return outstanding_reads_.find(addr) != outstanding_reads_.end();
}

bool SimEngine::admitLocked(uint64_t addr, bool is_write, uint64_t tag,
                            uint64_t issue_cycle) {
  // DRAMsim3 deadlocks if a channel write buffer is full (or forced
  // into write-drain) while its head write matches a pending read:
  // ScheduleTransaction only considers writes, hits the R→W check,
  // and never issues the read.  Keep the write in the frontend until
  // that read completes (vendored DRAMsim3 stays unmodified).
  if (is_write && readOutstandingLocked(addr)) {
    return false;
  }
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

bool SimEngine::tryAdmitLocked(uint64_t addr, bool is_write, uint64_t tag) {
  return admitLocked(addr, is_write, tag, cycle_);
}

uint64_t SimEngine::tick(uint64_t cycles) {
  std::lock_guard<std::mutex> lock(mutex_);
  AdvanceSpec spec;
  spec.max_ticks = cycles;
  return advanceLocked(spec);
}

void SimEngine::tickOnceLocked() {
  if (frontend_count_ > 0) {
    drainFrontendLocked();
  }
  dramsim_->ClockTick();
  ++cycle_;
  if (frontend_count_ > 0) {
    drainFrontendLocked();
  }
}

int SimEngine::channelOfLocked(uint64_t addr) const {
  return static_cast<int>((addr >> shift_bits_ >> ch_pos_) & ch_mask_);
}

int SimEngine::bucketOf(uint64_t addr, bool is_write) const {
  const int ch = channelOfLocked(addr);
  if (ch < 0 || ch >= num_channels_) {
    throw std::runtime_error("channel index out of range");
  }
  return ch * 2 + (is_write ? 1 : 0);
}

void SimEngine::pushFrontendLocked(uint64_t addr, bool is_write, uint64_t tag) {
  frontend_queues_[static_cast<size_t>(bucketOf(addr, is_write))].push_back(
      PendingTransaction{addr, is_write, tag, cycle_, park_seq_++});
  ++frontend_count_;
}

bool SimEngine::atCapLocked() const {
  return outstanding_cap_ > 0 && inFlightLocked() >= outstanding_cap_;
}

bool SimEngine::parkLocked(uint64_t addr, bool is_write, uint64_t tag) {
  if (atCapLocked()) {
    return false;
  }
  pushFrontendLocked(addr, is_write, tag);
  drainFrontendLocked();
  return true;
}

bool SimEngine::park(uint64_t addr, bool is_write, uint64_t tag) {
  std::lock_guard<std::mutex> lock(mutex_);
  return parkLocked(addr, is_write, tag);
}

uint64_t SimEngine::parkRange(uint64_t addr, uint64_t count, uint64_t stride,
                                 bool is_write, uint64_t tag) {
  std::lock_guard<std::mutex> lock(mutex_);
  uint64_t parked = 0;
  for (uint64_t i = 0; i < count; ++i) {
    if (atCapLocked()) {
      break;
    }
    pushFrontendLocked(addr, is_write, tag);
    addr += stride;
    ++parked;
  }
  if (parked > 0) {
    drainFrontendLocked();
  }
  return parked;
}

void SimEngine::setOutstandingCap(uint64_t cap) {
  std::lock_guard<std::mutex> lock(mutex_);
  outstanding_cap_ = cap;
}

uint64_t SimEngine::outstandingCap() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return outstanding_cap_;
}

int SimEngine::numChannels() const { return num_channels_; }

uint64_t SimEngine::memorySize() const { return memory_size_; }

int SimEngine::channelOf(uint64_t addr) const {
  std::lock_guard<std::mutex> lock(mutex_);
  return channelOfLocked(addr);
}

bool SimEngine::willAccept(uint64_t addr, bool is_write) const {
  std::lock_guard<std::mutex> lock(mutex_);
  return dramsim_->WillAcceptTransaction(addr, is_write);
}

uint64_t SimEngine::unmatchedCallbacks() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return unmatched_callbacks_;
}

uint64_t SimEngine::frontendBlockedWrites() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return frontendBlockedWritesLocked();
}

uint64_t SimEngine::frontendBlockedWritesLocked() const {
  uint64_t n = 0;
  for (int i = 1; i < num_buckets_; i += 2) {
    for (const PendingTransaction& t : frontend_queues_[static_cast<size_t>(i)]) {
      if (readOutstandingLocked(t.addr)) {
        ++n;
      }
    }
  }
  return n;
}

void SimEngine::drainFrontendLocked() {
  if (frontend_count_ == 0) {
    return;
  }
  const int n = num_buckets_;
  for (;;) {
    int best = -1;
    std::size_t best_idx = 0;
    uint64_t best_seq = ~uint64_t{0};
    for (int i = 0; i < n; ++i) {
      auto& q = frontend_queues_[static_cast<size_t>(i)];
      if (q.empty()) {
        continue;
      }
      const bool write_bucket = (i % 2) == 1;
      std::size_t idx = 0;
      for (const PendingTransaction& t : q) {
        if (t.seq >= best_seq) {
          if (!write_bucket) {
            break;
          }
          ++idx;
          continue;
        }
        if (t.is_write && readOutstandingLocked(t.addr)) {
          // Skip only the aliasing write; later writes on this channel
          // may still enter DRAMsim3 (avoids HOL-stalling the write
          // queue behind a DRAMSim3 R→W deadlock).
          if (!write_bucket) {
            break;
          }
          ++idx;
          continue;
        }
        if (!dramsim_->WillAcceptTransaction(t.addr, t.is_write)) {
          break;
        }
        best = i;
        best_idx = idx;
        best_seq = t.seq;
        break;
      }
    }
    if (best < 0) {
      return;
    }
    auto& q = frontend_queues_[static_cast<size_t>(best)];
    auto it = q.begin();
    std::advance(it, static_cast<std::ptrdiff_t>(best_idx));
    const PendingTransaction t = *it;
    if (!admitLocked(t.addr, t.is_write, t.tag, t.issue_cycle)) {
      return;
    }
    q.erase(it);
    --frontend_count_;
  }
}

uint64_t SimEngine::inFlightLocked() const {
  return num_outstanding_reads_ + num_outstanding_writes_ + frontend_count_;
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
    while (!tryAdmitLocked(addr, is_write, 0)) {
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
  AdvanceSpec tail;
  tail.max_ticks = max_drain_cycles;
  tail.stop_if_idle = true;
  advanceLocked(tail);
  return cycle_ - start;
}

uint64_t SimEngine::advanceUntilAccept(uint64_t addr, bool is_write,
                                      uint64_t max_cycles) {
  std::lock_guard<std::mutex> lock(mutex_);
  AdvanceSpec spec;
  spec.max_ticks = max_cycles;
  spec.stop_on_accept = true;
  spec.accept_addr = addr;
  spec.accept_write = is_write;
  return advanceLocked(spec);
}

uint64_t SimEngine::advanceUntilCompletion(uint64_t max_cycles) {
  std::lock_guard<std::mutex> lock(mutex_);
  AdvanceSpec spec;
  spec.max_ticks = max_cycles;
  spec.stop_if_idle = true;
  spec.stop_on_completion = true;
  return advanceLocked(spec);
}

uint64_t SimEngine::advanceTo(uint64_t target_cycle, bool stop_on_completion) {
  std::lock_guard<std::mutex> lock(mutex_);
  AdvanceSpec spec;
  spec.has_target = true;
  spec.target_cycle = target_cycle;
  spec.stop_on_completion = stop_on_completion;
  return advanceLocked(spec);
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

uint64_t SimEngine::advanceLocked(const AdvanceSpec& spec) {
  if (spec.reset_tag_done) {
    tag_done_ = false;
  }
  const uint64_t mark = completion_count_;
  uint64_t n = 0;
  while (true) {
    if (n >= spec.max_ticks) {
      break;
    }
    if (spec.has_target && cycle_ >= spec.target_cycle) {
      break;
    }
    if (spec.stop_if_idle && inFlightLocked() == 0) {
      break;
    }
    if (spec.stop_on_in_flight_below &&
        inFlightLocked() < spec.in_flight_below) {
      break;
    }
    if (spec.stop_on_tag_done && tag_done_) {
      break;
    }
    if (spec.stop_on_accept &&
        dramsim_->WillAcceptTransaction(spec.accept_addr, spec.accept_write)) {
      break;
    }
    tickOnceLocked();
    ++n;
    if (spec.stop_on_completion && completion_count_ > mark) {
      break;
    }
    if (spec.stop_on_tag_done && tag_done_) {
      break;
    }
  }
  return n;
}

uint64_t SimEngine::advanceUntil(uint64_t target_cycle, bool stop_on_tag_done,
                                 uint64_t max_cycles) {
  std::lock_guard<std::mutex> lock(mutex_);
  AdvanceSpec spec;
  spec.stop_on_tag_done = stop_on_tag_done;
  spec.reset_tag_done = true;
  if (target_cycle == std::numeric_limits<uint64_t>::max()) {
    spec.stop_if_idle = true;
    spec.max_ticks = max_cycles == 0 ? spec.max_ticks : max_cycles;
  } else {
    spec.has_target = true;
    spec.target_cycle = target_cycle;
  }
  return advanceLocked(spec);
}

uint64_t SimEngine::advanceBy(uint64_t cycles, bool stop_on_tag_done) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (cycles == 0) {
    return 0;
  }
  AdvanceSpec spec;
  spec.stop_on_tag_done = stop_on_tag_done;
  spec.reset_tag_done = true;
  uint64_t target = cycle_ + cycles;
  if (target < cycle_) {
    // Overflow: unbounded, no cap (same as advanceUntil UINT64_MAX, 0).
    spec.stop_if_idle = true;
  } else {
    spec.target_cycle = target;
    spec.has_target = true;
  }
  return advanceLocked(spec);
}

uint64_t SimEngine::frontendSize() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return frontend_count_;
}

uint64_t SimEngine::inFlight() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return inFlightLocked();
}

uint64_t SimEngine::advanceUntilInFlightBelow(uint64_t cap, uint64_t max_cycles) {
  std::lock_guard<std::mutex> lock(mutex_);
  AdvanceSpec spec;
  spec.max_ticks = max_cycles;
  spec.stop_if_idle = true;
  spec.stop_on_in_flight_below = true;
  spec.in_flight_below = cap;
  return advanceLocked(spec);
}

uint64_t SimEngine::drain(uint64_t max_cycles) {
  std::lock_guard<std::mutex> lock(mutex_);
  AdvanceSpec spec;
  spec.max_ticks = max_cycles;
  spec.stop_if_idle = true;
  return advanceLocked(spec);
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
  std::vector<Completion> keep;
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
  if (it == outstanding_reads_.end()) {
    ++unmatched_callbacks_;
    return;
  }
  uint64_t submit_cycle = it->second.front().first;
  uint64_t tag = it->second.front().second;
  it->second.pop();
  if (it->second.empty()) {
    outstanding_reads_.erase(it);
  }
  --num_outstanding_reads_;
  ++completion_count_;
  noteTagLocked(tag);
  collect(addr, submit_cycle, tag, false);
}

void SimEngine::onWriteComplete(uint64_t addr) {
  auto it = outstanding_writes_.find(addr);
  if (it == outstanding_writes_.end()) {
    ++unmatched_callbacks_;
    return;
  }
  uint64_t submit_cycle = it->second.front().first;
  uint64_t tag = it->second.front().second;
  it->second.pop();
  if (it->second.empty()) {
    outstanding_writes_.erase(it);
  }
  --num_outstanding_writes_;
  ++completion_count_;
  noteTagLocked(tag);
  collect(addr, submit_cycle, tag, true);
}

void SimEngine::collect(uint64_t addr, uint64_t submit_cycle, uint64_t tag,
                        bool is_write) {
  if (!collect_events_) {
    return;
  }
  // Callbacks fire during ClockTick, before tickOnceLocked increments
  // cycle_.  The DES-visible timestamp is the engine current_cycle after
  // that increment (matching Memory.current_cycle after wait()).
  events_.push_back(Completion{addr, cycle_ - submit_cycle, tag,
                                    cycle_ + 1, is_write});
}
