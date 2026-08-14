cmake_minimum_required(VERSION 3.15)
project(pydramsim3 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE "Release" CACHE STRING "Build type" FORCE)
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

add_subdirectory("${DRAMSIM3_DIR}" "${DRAMSIM3_DIR}/build")

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

target_link_libraries(_dramsim3 PRIVATE dramsim3)

# macOS uses @loader_path, not Linux's $ORIGIN, to resolve the bundled
# libdramsim3.dylib next to the module.
if(APPLE)
    set(PYDRAMSIM3_RPATH "@loader_path")
else()
    set(PYDRAMSIM3_RPATH "$ORIGIN")
endif()

set_target_properties(_dramsim3 PROPERTIES
    INSTALL_RPATH "${PYDRAMSIM3_RPATH}"
    BUILD_RPATH "${PYDRAMSIM3_RPATH}"
)

# DRAMsim3 declares no dllexport symbols; without this MSVC produces a DLL
# with no import library and linking _dramsim3 fails with LNK1181.
if(WIN32)
    set_target_properties(dramsim3 PROPERTIES WINDOWS_EXPORT_ALL_SYMBOLS ON)
endif()

add_custom_command(TARGET _dramsim3 POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        $<TARGET_FILE:dramsim3>
        $<TARGET_FILE_DIR:_dramsim3>/
)

install(TARGETS _dramsim3
        LIBRARY DESTINATION .
        RUNTIME DESTINATION .)

install(FILES $<TARGET_FILE:dramsim3>
        DESTINATION .)

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