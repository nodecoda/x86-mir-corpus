//===-- X86LegalizerTestSupport.h -------------------------------*- C++ -*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//
//
// Shared helpers for the X86 GlobalISel legalizer corpus drivers: subtarget
// configurations, LLT spelling, corpus reading, and opcode names.  The
// differential test keeps its private copies for now; this is the copy-first
// extraction that later stories can converge on.
//
//===----------------------------------------------------------------------===//

#ifndef LLVM_UNITTESTS_TARGET_X86_X86LEGALIZERTESTSUPPORT_H
#define LLVM_UNITTESTS_TARGET_X86_X86LEGALIZERTESTSUPPORT_H

#include "llvm/ADT/StringRef.h"
#include "llvm/CodeGenTypes/LowLevelType.h"
#include "llvm/Target/TargetMachine.h"
#include <memory>
#include <optional>
#include <string>

namespace llvm {

class X86Subtarget;
class TargetSubtargetInfo;

namespace x86legalizer {

struct Config {
  const char *ID;
  const char *Triple;
  const char *FS;
  // X86-only capability flags; unused by target-neutral configs.
  bool Is64;
  bool AVX512;
  bool VPOPCNTDQ;
  bool VLX;
  bool POPCNT;
};

const Config *findFullConfig(StringRef ID);
const Config *findFullConfig(StringRef Target, StringRef ID);

struct Sigma {
  std::unique_ptr<TargetMachine> TM;
  const TargetSubtargetInfo *STI = nullptr;
  unsigned PointerBits = 0;
};

Sigma makeSigma(const Config &C);

std::optional<LLT> parseCorpusLLT(StringRef S);
std::optional<std::string> readFile(StringRef Path);
std::optional<unsigned> opcodeForName(StringRef Name);

} // namespace x86legalizer
} // namespace llvm

#endif
