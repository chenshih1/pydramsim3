#include <pybind11/functional.h>
#include <pybind11/pybind11.h>

#include <functional>
#include <string>

#include "dramsim3_wrapper.hpp"

namespace py = pybind11;

// Convert a Python callable (or None) to a C++ std::function.
// None becomes a no-op lambda so DRAMsim3 always gets a valid callback.
static std::function<void(uint64_t)> to_callback(py::object obj) {
  if (obj.is_none()) {
    return [](uint64_t) {};
  }
  return obj.cast<std::function<void(uint64_t)>>();
}

PYBIND11_MODULE(_dramsim3, m) {
  m.doc() = "Python bindings for DRAMSim3 memory simulator";

  py::class_<DRAMsim3Wrapper>(m, "DRAMsim3Wrapper")
      .def(
          py::init([](const std::string &config_file,
                      const std::string &working_dir,
                      py::object read_complete, py::object write_complete) {
            return new DRAMsim3Wrapper(config_file, working_dir,
                                       to_callback(read_complete),
                                       to_callback(write_complete));
          }),
          py::arg("config_file"), py::arg("working_dir"),
          py::arg("read_complete") = py::none(),
          py::arg("write_complete") = py::none())
      .def_property_readonly("clock_period", &DRAMsim3Wrapper::clockPeriod)
      .def_property_readonly("queue_size", &DRAMsim3Wrapper::queueSize)
      .def_property_readonly("burst_size", &DRAMsim3Wrapper::burstSize)
      .def("print_stats", &DRAMsim3Wrapper::printStats)
      .def("reset_stats", &DRAMsim3Wrapper::resetStats)
      .def(
          "set_callbacks",
          [](DRAMsim3Wrapper &self, py::object read_complete,
             py::object write_complete) {
            self.setCallbacks(to_callback(read_complete),
                              to_callback(write_complete));
          },
          py::arg("read_complete") = py::none(),
          py::arg("write_complete") = py::none())
      .def("can_accept", &DRAMsim3Wrapper::canAccept, py::arg("addr"),
           py::arg("is_write"))
      .def("enqueue", &DRAMsim3Wrapper::enqueue, py::arg("addr"),
           py::arg("is_write"))
      .def("tick", &DRAMsim3Wrapper::tick);
}
