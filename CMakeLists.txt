cmake_minimum_required(VERSION 3.15)
project(pydramsim3 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE "Release" CACHE STRING "Build type" FORCE)
endif()

# Emit per-function/per-data sections and let the linker drop the ones we
# never call: the vendored simulator carries a main binary, thermal and
# test code that a wheel must not drag along.
if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    add_compile_options(-ffunction-sections -fdata-sections)
    if(APPLE)
        add_link_options("-Wl,-dead_strip")
    else()
        add_link_options("-Wl,--gc-sections")
    endif()
endif()

# Whole-program optimization: the simulation hot loop spans sim_engine.cpp
# and the vendored sources, so interprocedural inlining across that
# boundary is where LTO pays off.  Release-only so debug rebuilds stay
# fast.  GCC, Clang and MSVC all support it; check_ipo_supported degrades
# gracefully on toolchains that do not.
include(CheckIPOSupported)
check_ipo_supported(RESULT _ipo_supported)
if(_ipo_supported)
    set(CMAKE_INTERPROCEDURAL_OPTIMIZATION_RELEASE ON)
else()
    message(STATUS "LTO/IPO not supported by this toolchain; skipping")
endif()

set(DRAMSIM3_DIR "${CMAKE_CURRENT_SOURCE_DIR}/third_party/dramsim3")
if(NOT EXISTS "${DRAMSIM3_DIR}/CMakeLists.txt")
    message(FATAL_ERROR "DRAMSim3 not found at '${DRAMSIM3_DIR}'.\n"
                        "Please run: git submodule update --init")
endif()

# DRAMsim3 declares cmake_minimum_required(3.0), which CMake >= 4 rejects;
# allow old minimums so fresh toolchains (e.g. scikit-build-core's injected
# CMake) can configure the vendored submodule.
set(CMAKE_POLICY_VERSION_MINIMUM 3.5)

# Configure the vendored tree but exclude its targets from the default
# build: we only reuse its interface libraries (inih, format) and its
# source list for the static library below.
add_subdirectory("${DRAMSIM3_DIR}" "${CMAKE_BINARY_DIR}/dramsim3" EXCLUDE_FROM_ALL)
set_target_properties(dramsim3main dramsim3test PROPERTIES EXCLUDE_FROM_ALL TRUE)

# Link DRAMsim3 statically into the extension module: one DSO total, which
# lets LTO optimize across the whole simulator, removes the
# $ORIGIN/@loader_path rpath dance and the Windows dllexport workaround.
get_target_property(_DRAMSIM3_SRCS dramsim3 SOURCES)
set(_DRAMSIM3_SRCS_ABS)
foreach(_src IN LISTS _DRAMSIM3_SRCS)
    if(NOT IS_ABSOLUTE "${_src}")
        list(APPEND _DRAMSIM3_SRCS_ABS "${DRAMSIM3_DIR}/${_src}")
    else()
        list(APPEND _DRAMSIM3_SRCS_ABS "${_src}")
    endif()
endforeach()

add_library(dramsim3_static STATIC ${_DRAMSIM3_SRCS_ABS})
target_include_directories(dramsim3_static PUBLIC
    "${DRAMSIM3_DIR}/src"
    "${DRAMSIM3_DIR}/ext/headers"
    "${DRAMSIM3_DIR}/ext/fmt/include"
)
target_compile_definitions(dramsim3_static PUBLIC FMT_HEADER_ONLY=1)
target_compile_options(dramsim3_static PRIVATE -Wall)
target_link_libraries(dramsim3_static PUBLIC inih format)
set_target_properties(dramsim3_static PROPERTIES
    CXX_STANDARD 11
    CXX_STANDARD_REQUIRED YES
    CXX_EXTENSIONS NO
)

# scikit-build-core always injects -DPython_EXECUTABLE (the interpreter
# driving the build); only fall back to a discovery step for standalone
# CMake use.  Note: do NOT request the Development component here - recent
# manylinux images ship interpreters without libpython, which FindPython
# then fails on; pybind11 performs its own Python detection.
if(NOT Python_EXECUTABLE)
    find_package(Python REQUIRED COMPONENTS Interpreter)
endif()

execute_process(
    COMMAND "${Python_EXECUTABLE}" -c "
import sysconfig, site;
paths = set()
try:
    for p in site.getsitepackages():
        paths.add(p)
except Exception:
    pass
if hasattr(site, 'USER_SITE') and site.USER_SITE:
    paths.add(site.USER_SITE)
for key in ('purelib', 'platlib'):
    p = sysconfig.get_path(key)
    if p:
        paths.add(p)
print(';'.join(sorted(paths)))
"
    OUTPUT_VARIABLE _PYTHON_SITE_DIRS
    OUTPUT_STRIP_TRAILING_WHITESPACE
    ERROR_QUIET
)

if(_PYTHON_SITE_DIRS)
    list(APPEND CMAKE_PREFIX_PATH ${_PYTHON_SITE_DIRS})
endif()

find_package(pybind11 CONFIG QUIET)

if(NOT pybind11_FOUND)
    message(FATAL_ERROR
        "pybind11 not found for Python ${Python_VERSION}.\n"
        "Install with one of:\n"
        "  pip install \"pybind11>=2.11\"\n"
        "  conda install -c conda-forge \"pybind11>=2.11\"\n\n"
        "Note: System packages (e.g., apt install pybind11-dev) may be outdated."
    )
endif()

message(STATUS "Using pybind11 ${pybind11_VERSION}")

set(SRC_DIR "${CMAKE_CURRENT_SOURCE_DIR}/src/pydramsim3")
set(SOURCES
    "${SRC_DIR}/_dramsim3.cpp"
    "${SRC_DIR}/sim_engine.cpp"
)

pybind11_add_module(_dramsim3 MODULE ${SOURCES})

target_include_directories(_dramsim3 PRIVATE
    "${SRC_DIR}"
    "${DRAMSIM3_DIR}/src"
    "${DRAMSIM3_DIR}/ext/headers"
    "${DRAMSIM3_DIR}/ext/fmt/include"
)

target_link_libraries(_dramsim3 PRIVATE dramsim3_static)

if(MSVC)
    target_compile_options(_dramsim3 PRIVATE /W4)
else()
    target_compile_options(_dramsim3 PRIVATE -Wall -Wextra)
endif()

install(TARGETS _dramsim3
        LIBRARY DESTINATION .
        RUNTIME DESTINATION .)

install(
    FILES
        "${CMAKE_CURRENT_SOURCE_DIR}/src/pydramsim3/__init__.py"
        "${CMAKE_CURRENT_SOURCE_DIR}/src/pydramsim3/_dramsim3.pyi"
        "${CMAKE_CURRENT_SOURCE_DIR}/src/pydramsim3/py.typed"
    DESTINATION .
)

# Bundle DRAMsim3 config files so users can discover them via configs_dir()
install(
    DIRECTORY "${DRAMSIM3_DIR}/configs/"
    DESTINATION configs
    FILES_MATCHING PATTERN "*.ini"
)