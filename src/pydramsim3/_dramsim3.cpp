#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <cstdint>
#include <cstring>
#include <limits>
#include <stdexcept>

#include "sim_engine.hpp"

namespace py = pybind11;

using U64Array = py::array_t<uint64_t, py::array::c_style | py::array::forcecast>;
using BoolArray = py::array_t<bool, py::array::c_style | py::array::forcecast>;

static py::tuple takeCompletionsNp(
    const std::tuple<std::vector<uint64_t>, std::vector<uint64_t>,
                     std::vector<uint64_t>, std::vector<uint64_t>>& events) {
  const auto& addrs_v = std::get<0>(events);
  const auto& lats_v = std::get<1>(events);
  const auto& tags_v = std::get<2>(events);
  const auto& cycles_v = std::get<3>(events);
  py::array_t<uint64_t> addrs(addrs_v.size());
  py::array_t<uint64_t> lats(lats_v.size());
  py::array_t<uint64_t> tags(tags_v.size());
  py::array_t<uint64_t> cycles(cycles_v.size());
  if (!addrs_v.empty()) {
    std::memcpy(addrs.mutable_data(), addrs_v.data(),
                addrs_v.size() * sizeof(uint64_t));
    std::memcpy(lats.mutable_data(), lats_v.data(),
                lats_v.size() * sizeof(uint64_t));
    std::memcpy(tags.mutable_data(), tags_v.data(),
                tags_v.size() * sizeof(uint64_t));
    std::memcpy(cycles.mutable_data(), cycles_v.data(),
                cycles_v.size() * sizeof(uint64_t));
  }
  return py::make_tuple(addrs, lats, tags, cycles);
}

PYBIND11_MODULE(_dramsim3, m) {
  m.doc() = "Internal C++ engine behind pydramsim3.Memory";

  // High-performance engine: the hot loop (submission, backpressure waits,
  // batching, outstanding tracking, per-transaction latency) lives entirely
  // in C++.  Completions are exported in bulk via take_completions().
  // tick()/drain()/advance_until_accept()/run_trace() release the GIL.
  py::class_<SimEngine>(m, "SimEngine")
      .def(py::init<const std::string&, const std::string&, bool>(),
           py::arg("config_file"), py::arg("working_dir"),
           py::arg("collect_events") = true)
      .def("try_admit", &SimEngine::tryAdmit, py::arg("addr"),
           py::arg("is_write"), py::arg("tag") = 0,
           "Submit one transaction, optionally tagged with a request id "
           "that is returned with its completion event; returns False on "
           "backpressure.")
      .def(
          "tick",
          [](SimEngine& self, uint64_t cycles) -> uint64_t {
            if (cycles >= 64) {
              // Large batches: release the GIL while DRAMsim3 runs.
              py::gil_scoped_release release;
              return self.tick(cycles);
            }
            // Small batches: the GIL round-trip costs more than the run.
            return self.tick(cycles);
          },
          py::arg("cycles") = 1,
          "Advance *cycles* clock cycles; returns cycles advanced.")
      .def("drain", &SimEngine::drain, py::arg("max_cycles") = 10000000,
           py::call_guard<py::gil_scoped_release>(),
           "Tick until controller and frontend are idle; returns cycles used.")
      .def(
          "advance_until_in_flight_below",
          [](SimEngine& self, uint64_t cap, uint64_t max_cycles) {
            if (max_cycles >= 64) {
              py::gil_scoped_release release;
              return self.advanceUntilInFlightBelow(cap, max_cycles);
            }
            return self.advanceUntilInFlightBelow(cap, max_cycles);
          },
          py::arg("cap"), py::arg("max_cycles") = 10000000,
          "Tick until in-flight is below cap, idle, or max_cycles.")
      .def("advance_until_accept", &SimEngine::advanceUntilAccept,
           py::arg("addr"), py::arg("is_write"),
           py::arg("max_cycles") = 10000000,
           py::call_guard<py::gil_scoped_release>(),
           "Tick until DRAMsim3 will accept try_admit(addr, is_write); "
           "returns cycles used.")
      .def("advance_until_completion", &SimEngine::advanceUntilCompletion,
           py::arg("max_cycles") = 10000000,
           py::call_guard<py::gil_scoped_release>(),
           "Tick until the next read or write completion; returns cycles used.")
      .def("advance_to", &SimEngine::advanceTo, py::arg("target_cycle"),
           py::arg("stop_on_completion") = true,
           py::call_guard<py::gil_scoped_release>(),
           "Tick until current_cycle reaches target_cycle; optionally stop "
           "at the first new completion.  Returns cycles used.")
      .def(
          "advance_until",
          [](SimEngine& self, uint64_t target_cycle, bool stop_on_tag_done,
             uint64_t max_cycles) {
            constexpr uint64_t kGilReleaseTicks = 64;
            const uint64_t now = self.currentCycle();
            const bool long_run =
                target_cycle == std::numeric_limits<uint64_t>::max() ||
                target_cycle > now + kGilReleaseTicks;
            if (long_run) {
              py::gil_scoped_release release;
              return self.advanceUntil(target_cycle, stop_on_tag_done,
                                       max_cycles);
            }
            return self.advanceUntil(target_cycle, stop_on_tag_done,
                                     max_cycles);
          },
          py::arg("target_cycle"), py::arg("stop_on_tag_done") = true,
          py::arg("max_cycles") = 10000000,
          "Tick until target_cycle (UINT64_MAX = no deadline).  Unbounded "
          "jumps also stop after max_cycles (0 = no cap).  Finite deadlines "
          "ignore max_cycles.  With stop_on_tag_done, return when a "
          "set_tag_quota counter hits zero.")
      .def(
          "advance_by",
          [](SimEngine& self, uint64_t cycles, bool stop_on_tag_done) {
            if (cycles >= 64) {
              py::gil_scoped_release release;
              return self.advanceBy(cycles, stop_on_tag_done);
            }
            return self.advanceBy(cycles, stop_on_tag_done);
          },
          py::arg("cycles"), py::arg("stop_on_tag_done") = true,
          "Tick up to *cycles* from now; same stop_on_tag_done rules as "
          "advance_until.  Returns cycles executed.")
      .def("set_tag_quota", &SimEngine::setTagQuota, py::arg("tag"),
           py::arg("remaining"),
           "Remaining bursts for a logical request tag; 0 clears it.")
      .def("park", &SimEngine::park, py::arg("addr"),
           py::arg("is_write"), py::arg("tag") = 0,
           "Park on a frontend queue if the controller is full.  Returns "
           "False when outstanding_cap is full.  Latency is measured from "
           "this call.")
      .def("park_range", &SimEngine::parkRange, py::arg("addr"),
           py::arg("count"), py::arg("stride"), py::arg("is_write"),
           py::arg("tag") = 0, py::call_guard<py::gil_scoped_release>(),
           "Park up to count bursts at addr, addr+stride, ... then drain "
           "once.  Returns how many were parked.")
      .def_property("outstanding_cap", &SimEngine::outstandingCap,
                    &SimEngine::setOutstandingCap,
                    "Finite in-flight window (controller + frontend).  "
                    "0 = unbounded.")
      .def_property_readonly("num_channels", &SimEngine::numChannels,
                             "DRAMsim3 channel count from the .ini.")
      .def_property_readonly("memory_size", &SimEngine::memorySize,
                             "Mapped address space in bytes (channels × "
                             "channel_size).")
      .def("channel_of", &SimEngine::channelOf, py::arg("addr"),
           "DRAMsim3 channel index for this byte address.")
      .def("will_accept", &SimEngine::willAccept, py::arg("addr"),
           py::arg("is_write"),
           "DRAMsim3 WillAcceptTransaction for this address and direction.")
      .def_property_readonly("unmatched_callbacks",
                             &SimEngine::unmatchedCallbacks,
                             "Completion callbacks whose addr was not in the "
                             "outstanding map.")
      .def_property_readonly(
          "frontend_blocked_writes", &SimEngine::frontendBlockedWrites,
          "Parked writes waiting because a read to the same addr is in flight.")
      .def_property_readonly(
          "frontend_size", &SimEngine::frontendSize,
          "Number of transactions waiting on the software frontend queue.")
      .def(
          "run_trace",
          [](SimEngine& self, U64Array addrs, BoolArray writes,
             uint64_t gap_cycles, uint64_t max_drain_cycles) {
            if (addrs.size() != writes.size()) {
              throw std::invalid_argument(
                  "addrs and writes must have the same length");
            }
            py::gil_scoped_release release;
            return self.runTrace(addrs.data(), writes.data(), addrs.size(),
                                 gap_cycles, max_drain_cycles);
          },
          py::arg("addrs"), py::arg("writes"), py::arg("gap_cycles") = 0,
          py::arg("max_drain_cycles") = 10000000,
          "Drive a trace of (addr, is_write) pairs stored as numpy arrays "
          "(uint64 addrs, bool writes) entirely in C++: submission with "
          "backpressure, gap cycles, and final drain.  Returns total cycles "
          "elapsed.  Zero-copy when arrays are already uint64/bool and "
          "C-contiguous; the GIL is released for the whole run.")
      .def("set_collect", &SimEngine::setCollect, py::arg("collect"),
           "Enable/disable completion-event collection.")
      .def("take_read_completions", &SimEngine::takeReadCompletions,
           "Return and clear (addr, latency, tag, complete_cycle) read "
           "completions as Python lists.")
      .def("take_write_completions", &SimEngine::takeWriteCompletions,
           "Return and clear (addr, latency, tag, complete_cycle) write "
           "completions as Python lists.")
      .def(
          "take_read_completions_np",
          [](SimEngine& self) {
            return takeCompletionsNp(self.takeReadCompletions());
          },
          "Return and clear (addr, latency, tag, complete_cycle) read "
          "completions as numpy arrays.")
      .def(
          "take_write_completions_np",
          [](SimEngine& self) {
            return takeCompletionsNp(self.takeWriteCompletions());
          },
          "Return and clear (addr, latency, tag, complete_cycle) write "
          "completions as numpy arrays.")
      .def("take_completions", &SimEngine::takeCompletions,
           "Return and clear all completions in callback order as "
           "(addr, latency, tag, complete_cycle, is_write) lists.")
      .def_property_readonly("num_outstanding", &SimEngine::numOutstanding)
      .def_property_readonly("in_flight", &SimEngine::inFlight,
                             "DRAMsim3 outstanding plus frontend queue depth.")
      .def_property_readonly("num_outstanding_reads",
                             &SimEngine::numOutstandingReads)
      .def_property_readonly("num_outstanding_writes",
                             &SimEngine::numOutstandingWrites)
      .def_property_readonly("current_cycle", &SimEngine::currentCycle,
                             "Absolute simulation cycle (engine clock).")
      .def("print_stats", &SimEngine::printStats)
      .def("reset_stats", &SimEngine::resetStats)
      .def_property_readonly("clock_period", &SimEngine::clockPeriod)
      .def_property_readonly("queue_size", &SimEngine::queueSize)
      .def_property_readonly("burst_size", &SimEngine::burstSize);
}
