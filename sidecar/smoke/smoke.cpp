//===-- smoke.cpp -------------------------------------------------*- C++ -*-==//
//
// Sidecar build/link smoke test: construct a C2 subtarget, ask the frozen
// reference legalizer for one action, and print it.
//
//===----------------------------------------------------------------------===//

#include "X86LegalizerInfoReference.h"
#include "X86LegalizerTestSupport.h"
#include "X86Subtarget.h"
#include "X86TargetMachine.h"
#include "llvm/CodeGen/GlobalISel/LegalizerInfo.h"
#include "llvm/CodeGen/TargetOpcodes.h"
#include "llvm/Support/TargetSelect.h"
#include "llvm/Support/raw_ostream.h"

using namespace llvm;
using namespace llvm::x86legalizer;

static const char *actionName(LegalizeAction A) {
  switch (A) {
  case LegalizeAction::Legal:
    return "legal";
  case LegalizeAction::Lower:
    return "lower";
  case LegalizeAction::Libcall:
    return "libcall";
  case LegalizeAction::Custom:
    return "custom";
  case LegalizeAction::Unsupported:
    return "unsupported";
  }
  return "unknown";
}

int main() {
  LLVMInitializeX86TargetInfo();
  LLVMInitializeX86Target();
  LLVMInitializeX86TargetMC();

  const Config *C = findFullConfig("C2");
  if (!C) {
    errs() << "x86-mir-smoke: C2 config missing\n";
    return 1;
  }
  Sigma S = makeSigma(*C);
  if (!S.TM || !S.STI) {
    errs() << "x86-mir-smoke: cannot create C2 subtarget\n";
    return 1;
  }

  X86LegalizerInfoReference Ref(
      *S.STI, *static_cast<const X86TargetMachine *>(S.TM.get()));
  SmallVector<LLT, 2> Types = {LLT::scalar(32), LLT::scalar(32)};
  LegalityQuery Q(TargetOpcode::G_ADD, Types);
  LegalizeActionStep Step = Ref.getAction(Q);

  outs() << "{\"config\":\"C2\",\"opcode\":\"G_ADD\",\"types\":[\"s32\",\"s32\"],"
            "\"action\":\""
         << actionName(Step.Action) << "\"}\n";
  return 0;
}
