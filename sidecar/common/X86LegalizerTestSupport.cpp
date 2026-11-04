//===-- X86LegalizerTestSupport.cpp -----------------------------*- C++ -*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//

#include "X86LegalizerTestSupport.h"

#include "X86Subtarget.h"
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

const Config *findFullConfig(StringRef ID) {
  for (const Config &C : FullConfigs)
    if (ID == C.ID)
      return &C;
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
  S.STI = static_cast<const X86Subtarget *>(
      S.TM->getSubtargetImpl(*M.getFunction("f")));
  return S;
}

static std::optional<LLT> parseJSONLLT(StringRef S, unsigned PointerBits) {
  if (S.consume_front("p")) {
    unsigned AddressSpace = 0;
    if (S.getAsInteger(10, AddressSpace))
      return std::nullopt;
    return LLT::pointer(AddressSpace, PointerBits);
  }
  if (S.consume_front("v")) {
    const size_t Sep = S.find_first_of("ps");
    if (Sep == StringRef::npos)
      return std::nullopt;
    unsigned Lanes = 0, Elem = 0;
    if (S.take_front(Sep).getAsInteger(10, Lanes) ||
        S.drop_front(Sep + 1).getAsInteger(10, Elem))
      return std::nullopt;
    if (S[Sep] == 'p')
      return LLT::fixed_vector(Lanes, LLT::pointer(Elem, PointerBits));
    return LLT::fixed_vector(Lanes, Elem);
  }
  if (S.consume_front("s")) {
    unsigned Bits = 0;
    if (S.getAsInteger(10, Bits))
      return std::nullopt;
    return LLT::scalar(Bits);
  }
  return std::nullopt;
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
