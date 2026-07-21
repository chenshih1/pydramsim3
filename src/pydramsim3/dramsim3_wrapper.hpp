#ifndef __DRAMSIM3_WRAPPER_HH__
#define __DRAMSIM3_WRAPPER_HH__

#include <functional>
#include <memory>
#include <string>

#include "dramsim3.h"

class DRAMsim3Wrapper {
 private:
  std::unique_ptr<dramsim3::MemorySystem> dramsim;

  double _clockPeriod;

  unsigned int _queueSize;

  unsigned int _burstSize;

 public:
  /**
   * Create an instance of the DRAMsim3 multi-channel memory
   * controller using a specific config and system description.
   *
   * @param config_file Memory config file
   * @param working_dir Path pre-pended to config files
   */
  DRAMsim3Wrapper(const std::string& config_file,
                  const std::string& working_dir,
                  std::function<void(uint64_t)> read_cb,
                  std::function<void(uint64_t)> write_cb);

  /**
   * Print the stats gathered in DRAMsim3.
   */
  void printStats();

  /**
   * Reset stats (useful for fastforwarding switch)
   */
  void resetStats();

  /**
   * Set the callbacks to use for read and write completion.
   *
   * @param read_callback Callback used for read completions
   * @param write_callback Callback used for write completions
   */
  void setCallbacks(std::function<void(uint64_t)> read_complete,
                    std::function<void(uint64_t)> write_complete);

  /**
   * Determine if the controller can accept a new packet or not.
   *
   * @return true if the controller can accept transactions
   */
  bool canAccept(uint64_t addr, bool is_write) const;

  /**
   * Enqueue a packet. This assumes that canAccept has returned true.
   *
   * @param pkt Packet to turn into a DRAMsim3 transaction
   */
  void enqueue(uint64_t addr, bool is_write);

  /**
   * Get the internal clock period used by DRAMsim3, specified in
   * ns.
   *
   * @return The clock period of the DRAM interface in ns
   */
  double clockPeriod() const;

  /**
   * Get the transaction queue size used by DRAMsim3.
   *
   * @return The queue size counted in number of transactions
   */
  unsigned int queueSize() const;

  /**
   * Get the burst size in bytes used by DRAMsim3.
   *
   * @return The burst size in bytes (data width * burst length)
   */
  unsigned int burstSize() const;

  /**
   * Progress the memory controller one cycle
   */
  void tick();
};

#endif  // __DRAMSIM3_WRAPPER_HH__