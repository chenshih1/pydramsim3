#include "dramsim3_wrapper.hpp"

#include <cassert>
#include <fstream>

#include "dramsim3.h"

DRAMsim3Wrapper::DRAMsim3Wrapper(const std::string &config_file,
                                 const std::string &working_dir,
                                 const std::function<void(uint64_t)> read_cb,
                                 const std::function<void(uint64_t)> write_cb)
    : dramsim(dramsim3::GetMemorySystem(config_file, working_dir, read_cb,
                                        write_cb)),
      _clockPeriod(0.0),
      _queueSize(0),
      _burstSize(0) {
  if (!dramsim) {
    throw std::runtime_error("Failed to create DRAMsim3 MemorySystem");
  }
  double tck = dramsim->GetTCK(); 
  if (tck == 0.0) {
    throw std::runtime_error("DRAMsim3 wrapper failed to get clock.");
  }
  _clockPeriod = tck;

  unsigned int qs = dramsim->GetQueueSize();
  if (qs == 0) {
    throw std::runtime_error("DRAMsim3 wrapper failed to get queue size.");
  }
  _queueSize = qs;

  unsigned int bus = dramsim->GetBusBits();
  unsigned int burst = dramsim->GetBurstLength();
  if (bus == 0 || burst == 0) {
    throw std::runtime_error(
        "DRAMsim3 wrapper failed to get burst parameters.");
  }
  _burstSize = bus * burst / 8;
}

void DRAMsim3Wrapper::printStats() { dramsim->PrintStats(); }

void DRAMsim3Wrapper::resetStats() { dramsim->ResetStats(); }

void DRAMsim3Wrapper::setCallbacks(
    std::function<void(uint64_t)> read_complete,
    std::function<void(uint64_t)> write_complete) {
  dramsim->RegisterCallbacks(read_complete, write_complete);
}

bool DRAMsim3Wrapper::canAccept(uint64_t addr, bool is_write) const {
  return dramsim->WillAcceptTransaction(addr, is_write);
}

void DRAMsim3Wrapper::enqueue(uint64_t addr, bool is_write) {
  if (!dramsim->AddTransaction(addr, is_write)) {
    throw std::runtime_error("DRAMsim3 failed to enqueue transaction");
  }
}

double DRAMsim3Wrapper::clockPeriod() const { return _clockPeriod; }

unsigned int DRAMsim3Wrapper::queueSize() const { return _queueSize; }

unsigned int DRAMsim3Wrapper::burstSize() const { return _burstSize; }

void DRAMsim3Wrapper::tick() { dramsim->ClockTick(); }
