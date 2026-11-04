//===-- X86LegalizerTestSupport.cpp -----------------------------*- C++ -*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//

#include "X86LegalizerTestSupport.h"

#include "llvm/ADT/StringRef.h"
#include "llvm/ADT/StringSwitch.h"
#include "llvm/CodeGen/TargetOpcodes.h"
#include "llvm/IR/Function.h"
#include "llvm/IR/LLVMContext.h"
#include "llvm/IR/Module.h"
#include "llvm/MC/TargetRegistry.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Target/TargetOptions.h"
#include "llvm/TargetParser/Triple.h"

using namespace llvm;

namespace llvm {
namespace x86legalizer {

// The four fixed full-corpus configurations from the test spec.  They are a
// separate axis from the CTPOP sigma configurations: the ledger needs the
// negative guards (x87 without SSE, no CMOV) and the AVX-512 extensions
// (DQI/BWI/LZCNT/BMI) that the sigma array does not vary.
static const Config FullConfigs[4] = {
    {"C0", "i386", "-x87", false, false, false, false, false},
    {"C1", "x86_64", "", true, false, false, false, false},
    {"C2", "x86_64",
     "+sse,+sse2,+sse4.1,+avx,+avx2,+avx512f,+avx512dq,+avx512bw,+avx512vl,"
     "+popcnt,+avx512vpopcntdq,+lzcnt,+bmi,+cmov",
     true, true, true, true, true},
    {"C3", "i386", "+x87,+popcnt,+lzcnt,+bmi,+cmov", false, false, false, false,
     true},
};

// AArch64 configurations.  A0 is the NEON+FP baseline; A2 matches the
// corpus' witness config and enables every feature the legalizer reads.
static const Config AArch64Configs[4] = {
    {"A0", "aarch64", "+neon,+fp-armv8", true, false, false, false, false},
    {"A1", "aarch64", "+neon,+fp-armv8,+fullfp16", true, false, false, false,
     false},
    {"A2", "aarch64",
     "+neon,+fp-armv8,+fullfp16,+sve,+cssc,+rcpc3,+bf16,+lse,+mops", true,
     false, false, false, false},
    {"A3", "aarch64",
     "+neon,+fp-armv8,+fullfp16,+sve,+cssc,+rcpc3,+bf16,+lse,+mops,"
     "+outline-atomics",
     true, false, false, false, false},
};

const Config *findFullConfig(StringRef ID) {
  for (const Config &C : FullConfigs)
    if (ID == C.ID)
      return &C;
  return nullptr;
}

const Config *findFullConfig(StringRef Target, StringRef ID) {
  if (Target == "x86")
    return findFullConfig(ID);
  if (Target == "aarch64") {
    for (const Config &C : AArch64Configs)
      if (ID == C.ID)
        return &C;
  }
  return nullptr;
}

Sigma makeSigma(const Config &C) {
  Sigma S;
  Triple TT(C.Triple);
  std::string Err;
  const Target *T = TargetRegistry::lookupTarget("", TT, Err);
  if (!T)
    return S;
  TargetOptions Opts;
  S.TM.reset(T->createTargetMachine(TT, "generic", C.FS, Opts, std::nullopt));
  if (!S.TM)
    return S;
  static LLVMContext Ctx;
  static Module M("t", Ctx);
  if (!M.getFunction("f"))
    Function::Create(FunctionType::get(Type::getVoidTy(Ctx), false),
                     Function::ExternalLinkage, "f", M);
  S.STI = S.TM->getSubtargetImpl(*M.getFunction("f"));
  S.PointerBits = S.TM->createDataLayout().getPointerSizeInBits(0);
  return S;
}

static std::optional<LLT> parseJSONLLT(StringRef S, unsigned PointerBits) {
  auto parseElement = [&](StringRef E) -> std::optional<LLT> {
    if (E.consume_front("p")) {
      unsigned AddressSpace = 0;
      if (E.getAsInteger(10, AddressSpace))
        return std::nullopt;
      return LLT::pointer(AddressSpace, PointerBits);
    }
    if (E.consume_front("s")) {
      unsigned Bits = 0;
      if (E.getAsInteger(10, Bits))
        return std::nullopt;
      return LLT::scalar(Bits);
    }
    if (E.consume_front("i")) {
      unsigned Bits = 0;
      if (E.getAsInteger(10, Bits))
        return std::nullopt;
      // Folds to LLT::scalar when the target has not enabled extended LLT.
      return LLT::integer(Bits);
    }
    if (E == "bf16")
      return LLT::bfloat16();
    if (E == "ppcf128")
      return LLT::ppcf128();
    if (E.consume_front("f")) {
      unsigned Bits = 0;
      if (E.getAsInteger(10, Bits))
        return std::nullopt;
      switch (Bits) {
      case 16:
        return LLT::float16();
      case 32:
        return LLT::float32();
      case 64:
        return LLT::float64();
      case 80:
        return LLT::x86fp80();
      case 128:
        return LLT::float128();
      default:
        return std::nullopt;
      }
    }
    return std::nullopt;
  };

  bool Scalable = false;
  if (S.consume_front("nxv"))
    Scalable = true;
  else if (S.consume_front("v")) {
  } else {
    return parseElement(S);
  }
  // ``v<lanes><element>``: the element token starts after the decimal lanes.
  size_t LaneEnd = 0;
  while (LaneEnd != S.size() && S[LaneEnd] >= '0' && S[LaneEnd] <= '9')
    ++LaneEnd;
  unsigned Lanes = 0;
  if (LaneEnd == 0 || S.take_front(LaneEnd).getAsInteger(10, Lanes))
    return std::nullopt;
  std::optional<LLT> Elem = parseElement(S.drop_front(LaneEnd));
  if (!Elem)
    return std::nullopt;
  if (Scalable)
    return LLT::scalable_vector(Lanes, *Elem);
  return LLT::fixed_vector(Lanes, *Elem);
}

/// Corpus type names carry the pointer width ("p0:64", "v2p0:64"), while the
/// points documents spell pointers without it.  Strip the suffix and reuse the
/// points parser so the two readers cannot drift.
std::optional<LLT> parseCorpusLLT(StringRef S) {
  unsigned PointerBits = 64;
  const size_t Colon = S.rfind(':');
  if (Colon != StringRef::npos) {
    unsigned Bits = 0;
    if (S.drop_front(Colon + 1).getAsInteger(10, Bits))
      return std::nullopt;
    PointerBits = Bits;
    S = S.take_front(Colon);
  }
  return parseJSONLLT(S, PointerBits);
}

std::optional<std::string> readFile(StringRef Path) {
  auto Buffer = MemoryBuffer::getFile(Path);
  if (!Buffer)
    return std::nullopt;
  return (*Buffer)->getBuffer().str();
}

std::optional<unsigned> opcodeForName(StringRef Name) {
  using namespace TargetOpcode;
  return StringSwitch<std::optional<unsigned>>(Name)
#undef HANDLE_TARGET_OPCODE
#undef HANDLE_TARGET_OPCODE_MARKER
#define HANDLE_TARGET_OPCODE(OPC) .Case(#OPC, OPC)
#define HANDLE_TARGET_OPCODE_MARKER(IDENT, OPC)
#include "llvm/Support/TargetOpcodes.def"
      .Default(std::nullopt);
#undef HANDLE_TARGET_OPCODE
#undef HANDLE_TARGET_OPCODE_MARKER
}

} // namespace x86legalizer
} // namespace llvm
