cmake_minimum_required(VERSION 3.20)
project(x86-mir-corpus-sidecar LANGUAGES C CXX)

# Out-of-tree sidecar for the X86 legalizer corpus tool.
#
# Configure with the LLVM CMake package of the pinned build:
#
#   cmake -S sidecar -B .build -G Ninja \
#     -DLLVM_DIR=$X86_MIR_LLVM_BUILD/lib/cmake/llvm
#
# LLVM source headers for the X86 target internals are taken from the same
# source tree that produced the build (LLVM_BUILD_MAIN_SRC_DIR).  Override
# X86_SOURCE_DIR when using a relocated source tree.

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

if(NOT LLVM_DIR AND DEFINED ENV{LLVM_DIR})
  set(LLVM_DIR "$ENV{LLVM_DIR}" CACHE PATH "LLVM CMake package directory")
endif()
find_package(LLVM CONFIG REQUIRED)
message(STATUS "x86-mir-corpus: LLVM ${LLVM_PACKAGE_VERSION} from ${LLVM_DIR}")

set(X86_SOURCE_DIR
    "${LLVM_BUILD_MAIN_SRC_DIR}/lib/Target/X86"
    CACHE PATH "LLVM X86 target source directory (headers only)")

if(NOT EXISTS "${X86_SOURCE_DIR}/X86Subtarget.h")
  message(FATAL_ERROR
    "x86-mir-corpus: missing X86 source headers under ${X86_SOURCE_DIR}; "
    "pass -DX86_SOURCE_DIR=<llvm-src>/llvm/lib/Target/X86")
endif()

if(LLVM_ENABLE_ASSERTIONS)
  set(X86_MIR_ASSERTIONS_JSON true)
else()
  set(X86_MIR_ASSERTIONS_JSON false)
endif()
configure_file(
  "${CMAKE_CURRENT_SOURCE_DIR}/build-info.json.in"
  "${CMAKE_BINARY_DIR}/sidecar-build-info.json"
  @ONLY)

set(X86_MIR_INCLUDES
    ${LLVM_INCLUDE_DIRS}
    "${LLVM_BINARY_DIR}/lib/Target/X86"
    "${LLVM_BINARY_DIR}/lib/Target/AArch64"
    "${X86_SOURCE_DIR}"
    "${CMAKE_CURRENT_SOURCE_DIR}/common"
    "${CMAKE_CURRENT_SOURCE_DIR}/reference")

add_library(x86mir_common STATIC common/X86LegalizerTestSupport.cpp)
add_library(x86mir_reference STATIC reference/X86LegalizerInfoReference.cpp)
set(X86_MIR_ASSERT_FLAGS)
if(LLVM_ENABLE_ASSERTIONS)
  # LegalizeRuleSet has assert-only members.  A client built with NDEBUG has a
  # different layout than an assertions-enabled libLLVM, so keep asserts on.
  list(APPEND X86_MIR_ASSERT_FLAGS -UNDEBUG)
endif()

foreach(target IN ITEMS x86mir_common x86mir_reference)
  target_include_directories(${target} PUBLIC ${X86_MIR_INCLUDES})
  target_compile_definitions(${target} PUBLIC ${LLVM_DEFINITIONS})
  target_compile_options(${target} PRIVATE
    ${X86_MIR_ASSERT_FLAGS} -fno-rtti -Wall -Wextra)
endforeach()

llvm_map_components_to_libnames(X86_MIR_LLVM_LIBS
  X86CodeGen X86Desc X86Info AArch64CodeGen AArch64Desc AArch64Info
  GlobalISel CodeGen CodeGenTypes MC MIRParser Support TargetParser Analysis
  Core)

add_executable(x86-mir-oracle oracle/CorpusOracle.cpp)
target_link_libraries(x86-mir-oracle PRIVATE
  x86mir_reference x86mir_common ${X86_MIR_LLVM_LIBS})
target_compile_definitions(x86-mir-oracle PRIVATE ${LLVM_DEFINITIONS})
target_compile_options(x86-mir-oracle PRIVATE
  ${X86_MIR_ASSERT_FLAGS} -fno-rtti -Wall -Wextra)

add_executable(x86-mir-run mir/MIRRunner.cpp)
target_link_libraries(x86-mir-run PRIVATE
  x86mir_reference x86mir_common ${X86_MIR_LLVM_LIBS})
target_compile_definitions(x86-mir-run PRIVATE ${LLVM_DEFINITIONS})
target_compile_options(x86-mir-run PRIVATE
  ${X86_MIR_ASSERT_FLAGS} -fno-rtti -Wall -Wextra)

add_executable(x86-mir-smoke smoke/smoke.cpp)
target_link_libraries(x86-mir-smoke PRIVATE
  x86mir_reference x86mir_common ${X86_MIR_LLVM_LIBS})
target_compile_definitions(x86-mir-smoke PRIVATE ${LLVM_DEFINITIONS})
target_compile_options(x86-mir-smoke PRIVATE
  ${X86_MIR_ASSERT_FLAGS} -fno-rtti -Wall -Wextra)
