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

add_subdirectory("${DRAMSIM3_DIR}" "${DRAMSIM3_DIR}/build")

find_package(Python REQUIRED COMPONENTS Interpreter Development)

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

set_target_properties(_dramsim3 PROPERTIES
    INSTALL_RPATH "$ORIGIN"
    BUILD_RPATH "$ORIGIN"
)

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