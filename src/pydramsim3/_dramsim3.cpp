#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <cstdint>
#include <cstring>
#include <stdexcept>

#include "sim_engine.hpp"

namespace py = pybind11;

using U64Array = py::array_t<uint64_t, py::array::c_style | py::array::forcecast>;
using BoolArray = py::array_t<bool, py::array::c_style | py::array::forcecast>;

// Export a (addrs, lats, tags) event buffer as numpy arrays and clear it.
static py::tuple take_events_np(
    const std::tuple<std::vector<uint64_t>, std::vector<uint64_t>,
                     std::vector<uint64_t>>& events) {
  const auto& addrs_v = std::get<0>(events);
  const auto& lats_v = std::get<1>(events);
  const auto& tags_v = std::get<2>(events);
  py::array_t<uint64_t> addrs(addrs_v.size());
  py::array_t<uint64_t> lats(lats_v.size());
  py::array_t<uint64_t> tags(tags_v.size());
  if (!addrs_v.empty()) {
    std::memcpy(addrs.mutable_data(), addrs_v.data(),
                addrs_v.size() * sizeof(uint64_t));
    std::memcpy(lats.mutable_data(), lats_v.data(),
                lats_v.size() * sizeof(uint64_t));
    std::memcpy(tags.mutable_data(), tags_v.data(),
                tags_v.size() * sizeof(uint64_t));
  }
  return py::make_tuple(addrs, lats, tags);
}

PYBIND11_MODULE(_dramsim3, m) {
  m.doc() = "Python bindings for DRAMsim3 memory simulator";

  // High-performance engine: the hot loop (submission, backpressure waits,
  // batching, outstanding tracking, per-transaction latency) lives entirely
  // in C++.  Completion events are exported in bulk as (addr, latency)
  // pairs via take_read_events()/take_write_events().  tick()/drain()/
  // tick_until_capacity()/run_trace() release the GIL while running.
  py::class_<SimEngine>(m, "SimEngine")
      .def(py::init<const std::string&, const std::string&, bool>(),
           py::arg("config_file"), py::arg("working_dir"),
           py::arg("collect_events") = true)
      .def("try_enqueue", &SimEngine::tryEnqueue, py::arg("addr"),
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
           "Tick until no transactions are outstanding; returns cycles used.")
      .def("tick_until_capacity", &SimEngine::tickUntilCapacity,
           py::arg("addr"), py::arg("is_write"),
           py::arg("max_cycles") = 10000000,
           py::call_guard<py::gil_scoped_release>(),
           "Tick until the next try_enqueue(addr, is_write) can succeed; "
           "returns cycles used.")
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
      .def("take_read_events", &SimEngine::takeReadEvents,
           "Return and clear collected (addr, latency, tag) read "
           "completions as Python lists.")
      .def("take_write_events", &SimEngine::takeWriteEvents,
           "Return and clear collected (addr, latency, tag) write "
           "completions as Python lists.")
      .def(
          "take_read_events_np",
          [](SimEngine& self) { return take_events_np(self.takeReadEvents()); },
          "Return and clear collected (addr, latency, tag) read "
          "completions as numpy arrays.")
      .def(
          "take_write_events_np",
          [](SimEngine& self) {
            return take_events_np(self.takeWriteEvents());
          },
          "Return and clear collected (addr, latency, tag) write "
          "completions as numpy arrays.")
      .def("num_outstanding", &SimEngine::numOutstanding)
      .def("num_outstanding_reads", &SimEngine::numOutstandingReads)
      .def("num_outstanding_writes", &SimEngine::numOutstandingWrites)
      .def_property_readonly("current_cycle", &SimEngine::currentCycle,
                             "Absolute simulation cycle (engine clock).")
      .def("print_stats", &SimEngine::printStats)
      .def("reset_stats", &SimEngine::resetStats)
      .def_property_readonly("clock_period", &SimEngine::clockPeriod)
      .def_property_readonly("queue_size", &SimEngine::queueSize)
      .def_property_readonly("burst_size", &SimEngine::burstSize);
}
