//===-- MIRRunner.cpp -------------------------------------------*- C++ -*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//
//
// Corpus driver: turn one legalizer query atom into a minimal real
// MachineFunction, verify it, run legalizeMachineFunction, and emit one
// explicit outcome per atom as JSONL.
//
// Construction is descriptor-driven: corpus types are assigned by
// MCOperandInfo::getGenericTypeIndex(), then the descriptor decides which
// operands are defs and uses.  Only the shapes that need extra structure
// (variadic build/merge/unmerge/concat, immediates, memory operands) are
// special-cased.  Everything outside the reviewed construction policy becomes
// an explicit `unbuildable` outcome with a stable reason.
//
// Run it with:
//
//   X86_LEGALIZER_MIR_CORPUS_IN=<in.json> \
//   X86_LEGALIZER_MIR_OUT=<out.jsonl> \
//   x86-mir-run
//
// Infrastructure problems (missing/invalid environment, unreadable or
// malformed corpus, unusable subtarget) print an
// "X86_LEGALIZER_MIR_INFRA_ERROR: " line on stderr and exit 2.  A skipped run
// must not look like a clean report.
//
//===----------------------------------------------------------------------===//

#include "X86LegalizerInfoReference.h"
#include "X86LegalizerTestSupport.h"
#include "X86Subtarget.h"
#include "X86TargetMachine.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/ADT/StringRef.h"
#include "llvm/CodeGen/GlobalISel/GISelChangeObserver.h"
#include "llvm/CodeGen/GlobalISel/GISelValueTracking.h"
#include "llvm/CodeGen/GlobalISel/Legalizer.h"
#include "llvm/CodeGen/GlobalISel/LegalizerInfo.h"
#include "llvm/CodeGen/GlobalISel/LostDebugLocObserver.h"
#include "llvm/CodeGen/GlobalISel/MachineIRBuilder.h"
#include "llvm/CodeGen/GlobalISel/Utils.h"
#include "llvm/CodeGen/MachineBasicBlock.h"
#include "llvm/CodeGen/MachineFunction.h"
#include "llvm/CodeGen/MachineMemOperand.h"
#include "llvm/CodeGen/MachineModuleInfo.h"
#include "llvm/CodeGen/MachineRegisterInfo.h"
#include "llvm/CodeGen/TargetOpcodes.h"
#include "llvm/IR/Constants.h"
#include "llvm/IR/DerivedTypes.h"
#include "llvm/IR/Function.h"
#include "llvm/IR/InstrTypes.h"
#include "llvm/IR/LLVMContext.h"
#include "llvm/IR/Module.h"
#include "llvm/IR/Type.h"
#include "llvm/MC/MCInstrDesc.h"
#include "llvm/MC/MCInstrInfo.h"
#include "llvm/Support/ErrorHandling.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/MathExtras.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/TargetSelect.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/TargetParser/Triple.h"
#include <cstdlib>
#include <map>
#include <optional>
#include <string>
#include <vector>

using namespace llvm;
using namespace llvm::x86legalizer;

namespace {

[[noreturn]] void infraError(const Twine &Msg) {
  errs() << "X86_LEGALIZER_MIR_INFRA_ERROR: " << Msg << "\n";
  errs().flush();
  std::exit(2);
}

std::string requireEnv(const char *Name) {
  const char *Value = std::getenv(Name);
  if (!Value || !*Value)
    infraError(Twine("missing environment variable ") + Name);
  return Value;
}

unsigned long parseMaxSteps() {
  const char *Value = std::getenv("X86_LEGALIZER_MIR_MAX_STEPS");
  if (!Value || !*Value)
    return 10000;
  unsigned long Steps = 0;
  if (StringRef(Value).getAsInteger(10, Steps))
    infraError(Twine("invalid X86_LEGALIZER_MIR_MAX_STEPS: ") + Value);
  return Steps;
}

unsigned long parseStartIndex() {
  const char *Value = std::getenv("X86_LEGALIZER_MIR_START_INDEX");
  if (!Value || !*Value)
    return 0;
  unsigned long Index = 0;
  if (StringRef(Value).getAsInteger(10, Index))
    infraError(Twine("invalid X86_LEGALIZER_MIR_START_INDEX: ") + Value);
  return Index;
}

struct Shard {
  unsigned Index = 0;
  unsigned Count = 1;
};

Shard parseShard() {
  const char *Value = std::getenv("X86_LEGALIZER_MIR_SHARD");
  StringRef Text = Value && *Value ? Value : "0/1";
  auto [IndexText, CountText] = Text.split('/');
  Shard S;
  if (IndexText.empty() || CountText.empty() ||
      IndexText.getAsInteger(10, S.Index) ||
      CountText.getAsInteger(10, S.Count) || S.Count == 0 || S.Index >= S.Count)
    infraError(Twine("invalid X86_LEGALIZER_MIR_SHARD (expected i/N): ") +
               Text);
  return S;
}

enum class RulesKind { Production, Reference };

RulesKind parseRules() {
  const char *Value = std::getenv("X86_LEGALIZER_MIR_RULES");
  StringRef Text = Value && *Value ? Value : "production";
  if (Text == "production")
    return RulesKind::Production;
  if (Text == "reference")
    return RulesKind::Reference;
  infraError(Twine("invalid X86_LEGALIZER_MIR_RULES (expected production or "
                   "reference): ") +
             Text);
}

std::string jsonEscape(StringRef S) {
  std::string Out;
  Out.reserve(S.size() + 8);
  for (char C : S) {
    switch (C) {
    case '"':
      Out += "\\\"";
      break;
    case '\\':
      Out += "\\\\";
      break;
    case '\n':
      Out += "\\n";
      break;
    case '\r':
      Out += "\\r";
      break;
    case '\t':
      Out += "\\t";
      break;
    default:
      Out += C;
      break;
    }
  }
  return Out;
}

std::string firstLine(StringRef Text, size_t MaxLen = 400) {
  // MachineVerifier::report() emits the banner and a full MIR dump before the
  // actual diagnostic; prefer the "*** Bad machine code: ..." line and fall
  // back to the first non-empty line when it is absent.
  StringRef Line;
  const StringRef Marker = "*** Bad machine code: ";
  const size_t MarkerPos = Text.find(Marker);
  if (MarkerPos != StringRef::npos) {
    Line = Text.substr(MarkerPos).split('\n').first;
  } else {
    const size_t Start = Text.find_first_not_of("\r\n");
    if (Start == StringRef::npos)
      return "";
    Line = Text.substr(Start).split('\n').first;
  }
  if (Line.size() > MaxLen)
    Line = Line.take_front(MaxLen);
  return Line.str();
}

/// Counts the observer callbacks the legalizer produces and aborts the process
/// once the budget is exceeded.  The abort deliberately exits 1 through
/// report_fatal_error so the supervisor can classify a livelock from stderr.
class StepBudgetObserver : public GISelChangeObserver {
  StringRef AtomID;
  unsigned long MaxSteps;
  unsigned long Steps = 0;

  void bump() {
    ++Steps;
    if (Steps > MaxSteps)
      report_fatal_error(Twine("legalizer step budget exceeded: atom=") +
                             AtomID + " steps=" + Twine(Steps),
                         /*GenCrashDiag=*/false);
  }

public:
  StepBudgetObserver(StringRef AtomID, unsigned long MaxSteps)
      : AtomID(AtomID), MaxSteps(MaxSteps) {}

  unsigned long getSteps() const { return Steps; }

  void createdInstr(MachineInstr &) override { bump(); }
  void erasingInstr(MachineInstr &) override { bump(); }
  void changingInstr(MachineInstr &) override { bump(); }
  void changedInstr(MachineInstr &) override {}
};

//===----------------------------------------------------------------------===//
// Generic opcode type contracts.
//
// A corpus atom assigns a type to every descriptor generic-type index
// independently, so it can describe an instruction that no IR producer can
// create: a pointer carry, a non-i1 icmp result, a pointer shift operand.
// Building one of those reaches a legalizer helper whose assertions document
// the invariant, the driver aborts, and the shard has to be resumed.  Check
// the invariant up front and report a construction problem instead, so every
// atom still gets exactly one explicit outcome.

enum class SlotCheck {
  /// Every type slot: scalar or vector integer, never a pointer.
  AllInt,
  /// This slot: scalar or vector integer, never a pointer.
  Int,
  /// i1 scalar, or <N x i1> with slot 0's lane count.
  CarryOf0,
  /// Same scalar-ness and lane count as slot 0.
  ShapeOf0,
  /// i1 scalar when slot 1 is scalar, else <N x i1> with slot 1's lanes.
  ICmpResult,
  /// i1 or <N x i1>; a vector condition must match slot 0's lane count.
  SelectCond,
};

struct SlotRule {
  unsigned Index;
  SlotCheck Check;
  const char *Role;
};

struct OpcodeContract {
  StringLiteral Name;
  ArrayRef<SlotRule> Rules;
};

bool isIntLike(const LLT &Ty) {
  return !Ty.isPointer() && !Ty.isPointerVector();
}

bool isI1Like(const LLT &Ty) {
  return isIntLike(Ty) && Ty.getScalarSizeInBits() == 1;
}

bool sameShape(const LLT &A, const LLT &B) {
  if (A.isVector() != B.isVector())
    return false;
  return !A.isVector() || A.getNumElements() == B.getNumElements();
}

std::string describeType(const LLT &Ty) {
  std::string Text;
  raw_string_ostream OS(Text);
  Ty.print(OS);
  return OS.str();
}

std::optional<std::string> contractFailure(unsigned Index, const char *Role,
                                           const char *Expected,
                                           const LLT &Actual) {
  return "type contract: slot " + std::to_string(Index) + " (" + Role +
         ") must be " + Expected + "; got " + describeType(Actual);
}

const SlotRule CarryRules[] = {{0, SlotCheck::Int, "result"},
                               {1, SlotCheck::CarryOf0, "carry"}};
const SlotRule UnaryIntRules[] = {{0, SlotCheck::Int, "result"},
                                  {1, SlotCheck::ShapeOf0, "source"}};
const SlotRule CompareRules[] = {{0, SlotCheck::ICmpResult, "result"}};
const SlotRule SelectRules[] = {{1, SlotCheck::SelectCond, "condition"}};
const SlotRule SextInRegRules[] = {{0, SlotCheck::Int, "operand"}};
const SlotRule IntegerOnlyRules[] = {{0, SlotCheck::AllInt, "operands"}};

const OpcodeContract Contracts[] = {
    {"G_UADDE", CarryRules},      {"G_UADDO", CarryRules},
    {"G_USUBE", CarryRules},      {"G_USUBO", CarryRules},
    {"G_SADDE", CarryRules},      {"G_SADDO", CarryRules},
    {"G_SSUBE", CarryRules},      {"G_SSUBO", CarryRules},
    {"G_CTLZ", UnaryIntRules},    {"G_CTLZ_ZERO_POISON", UnaryIntRules},
    {"G_CTTZ", UnaryIntRules},    {"G_CTTZ_ZERO_POISON", UnaryIntRules},
    {"G_CTPOP", UnaryIntRules},   {"G_ICMP", CompareRules},
    {"G_FCMP", CompareRules},     {"G_SELECT", SelectRules},
    {"G_SEXT_INREG", SextInRegRules},
    {"G_ADD", IntegerOnlyRules},  {"G_SUB", IntegerOnlyRules},
    {"G_MUL", IntegerOnlyRules},  {"G_SDIV", IntegerOnlyRules},
    {"G_UDIV", IntegerOnlyRules}, {"G_SREM", IntegerOnlyRules},
    {"G_UREM", IntegerOnlyRules}, {"G_AND", IntegerOnlyRules},
    {"G_OR", IntegerOnlyRules},   {"G_XOR", IntegerOnlyRules},
    {"G_SHL", IntegerOnlyRules},  {"G_LSHR", IntegerOnlyRules},
    {"G_ASHR", IntegerOnlyRules}, {"G_ABS", IntegerOnlyRules},
    {"G_SADDSAT", IntegerOnlyRules}, {"G_UADDSAT", IntegerOnlyRules},
    {"G_SSUBSAT", IntegerOnlyRules}, {"G_USUBSAT", IntegerOnlyRules},
    {"G_SSHLSAT", IntegerOnlyRules}, {"G_USHLSAT", IntegerOnlyRules},
};

/// Returns a stable reason when the atom's types violate the opcode contract.
std::optional<std::string> checkTypeContract(StringRef Opcode,
                                             ArrayRef<LLT> Types) {
  for (const OpcodeContract &Contract : Contracts) {
    if (Contract.Name != Opcode)
      continue;
    for (const SlotRule &Rule : Contract.Rules) {
      if (Rule.Check == SlotCheck::AllInt) {
        for (unsigned I = 0; I != Types.size(); ++I)
          if (!isIntLike(Types[I]))
            return contractFailure(I, Rule.Role,
                                   "an integer or integer vector, not a "
                                   "pointer",
                                   Types[I]);
        continue;
      }
      if (Rule.Index >= Types.size())
        return "type contract: missing type slot " +
               std::to_string(Rule.Index) + " (" + Rule.Role + ")";
      const LLT &Ty = Types[Rule.Index];
      switch (Rule.Check) {
      case SlotCheck::AllInt:
        continue;
      case SlotCheck::Int:
        if (!isIntLike(Ty))
          return contractFailure(Rule.Index, Rule.Role,
                                 "an integer or integer vector, not a pointer",
                                 Ty);
        break;
      case SlotCheck::CarryOf0: {
        const LLT &Res = Types[0];
        if (Res.isVector()) {
          if (!Ty.isVector() || !isI1Like(Ty) ||
              Ty.getNumElements() != Res.getNumElements())
            return contractFailure(
                Rule.Index, Rule.Role,
                "<N x i1> with the result's lane count", Ty);
        } else if (Ty.isVector() || !isI1Like(Ty)) {
          return contractFailure(Rule.Index, Rule.Role, "i1", Ty);
        }
        break;
      }
      case SlotCheck::ShapeOf0: {
        const LLT &Dst = Types[0];
        if (!sameShape(Dst, Ty))
          return contractFailure(
              Rule.Index, Rule.Role,
              "the same scalar/vector shape as slot 0", Ty);
        break;
      }
      case SlotCheck::ICmpResult: {
        if (Rule.Index + 1 >= Types.size())
          return "type contract: compare atom has no operand type slot";
        const LLT &Op = Types[Rule.Index + 1];
        if (Op.isVector()) {
          if (!Ty.isVector() || !isI1Like(Ty) ||
              Ty.getNumElements() != Op.getNumElements())
            return contractFailure(Rule.Index, Rule.Role,
                                   "<N x i1> with the operand lane count", Ty);
        } else if (Ty.isVector() || !isI1Like(Ty)) {
          return contractFailure(Rule.Index, Rule.Role, "i1", Ty);
        }
        break;
      }
      case SlotCheck::SelectCond: {
        if (!isI1Like(Ty))
          return contractFailure(Rule.Index, Rule.Role, "i1 or <N x i1>", Ty);
        const LLT &Values = Types[0];
        if (Ty.isVector() &&
            (!Values.isVector() ||
             Values.getNumElements() != Ty.getNumElements()))
          return contractFailure(Rule.Index, Rule.Role,
                                 "a vector with the values' lane count", Ty);
        break;
      }
      }
    }
    return std::nullopt;
  }
  return std::nullopt;
}

struct MemSpec {
  LLT Type;
  uint64_t Align;
};

struct Atom {
  std::string ID;
  std::string OpcodeName;
  std::vector<LLT> Types;
  std::optional<MemSpec> Mem;
  // Set when query.mem cannot be turned into a MachineMemOperand.
  // This is a per-atom construction problem, not an infrastructure error.
  std::optional<std::string> UnbuildableReason;
};

struct Outcome {
  const char *Kind;
  unsigned long Steps;
  const char *DetailCode;
  std::string Detail;
  /// For ``failed``: the opcode of the instruction the legalizer actually
  /// failed on.  The atom's own opcode is not enough, because auxiliary
  /// producers (e.g. injected G_IMPLICIT_DEFs) can fail first.
  std::string FailedOnOpcode;
};

void emitOutcome(raw_fd_ostream &OS, const Atom &A, unsigned Index,
                 const Outcome &O) {
  OS << "{\"event\":\"outcome\",\"id\":\"" << jsonEscape(A.ID)
     << "\",\"index\":" << Index << ",\"opcode\":\"" << jsonEscape(A.OpcodeName)
     << "\",\"outcome\":\"" << O.Kind << "\",\"steps\":" << O.Steps
     << ",\"detail_code\":\"" << jsonEscape(O.DetailCode) << "\",\"detail\":\""
     << jsonEscape(O.Detail) << "\"";
  if (!O.FailedOnOpcode.empty())
    OS << ",\"failed_on_opcode\":\"" << jsonEscape(O.FailedOnOpcode) << "\"";
  OS << "}\n";
  OS.flush();
}

void emitMarker(raw_fd_ostream &OS, StringRef Event, const Atom &A,
                unsigned Index) {
  OS << "{\"event\":\"" << Event << "\",\"id\":\"" << jsonEscape(A.ID)
     << "\",\"index\":" << Index << "}\n";
  OS.flush();
}

bool isLoadStoreOpcode(StringRef Name) {
  return Name == "G_LOAD" || Name == "G_STORE" || Name == "G_FPEXTLOAD" ||
         Name == "G_SEXTLOAD" || Name == "G_ZEXTLOAD" ||
         Name == "G_FPTRUNCSTORE";
}

/// Reviewed unbuildable policy (PRD 4.4).  These shapes have no construction
/// plan that keeps the legalizer contract honest, so they are reported with a
/// stable, non-empty reason instead of being silently skipped.
std::optional<std::string> fixedUnbuildableReason(StringRef Name) {
  if (Name == "G_BR")
    return "control flow: G_BR has a branch target";
  if (Name == "G_BRCOND")
    return "control flow: G_BRCOND has a branch target";
  if (Name == "G_PHI")
    return "control flow: G_PHI needs predecessor edges";
  if (Name.starts_with("G_INTRINSIC"))
    return "intrinsic opcode has no intrinsic ID";
  if (Name == "G_GLOBAL_VALUE" || Name == "G_FRAME_INDEX" ||
      Name == "G_CONSTANT_POOL")
    return "relocation-bearing opcode is unbuildable";
  if (Name == "G_MEMCPY" || Name == "G_MEMMOVE" || Name == "G_MEMSET")
    return "libcall lowering disabled";
  if (Name == "G_STACKSAVE" || Name == "G_STACKRESTORE" ||
      Name == "G_GET_ROUNDING" || Name == "G_SET_ROUNDING" ||
      Name == "G_INVOKE_REGION_START")
    return "stack or rounding special opcode is unbuildable";
  if (Name == "G_DYN_STACKALLOC")
    return "dynamic stack allocation is unbuildable";
  if (Name == "G_FPOWI")
    return "G_FPOWI type1 has no reviewed fill";
  if (Name == "G_TRAP" || Name == "G_DEBUGTRAP" || Name == "G_UBSANTRAP")
    return "trap opcode is unbuildable";
  return std::nullopt;
}

/// Assigns corpus types to every descriptor generic-type index, applying only
/// the reviewed fill rules.  A missing index with no fill is unbuildable; a
/// corpus type vector that is too long is a readback mismatch.
std::optional<std::string>
assignTypes(const Atom &A, const MCInstrDesc &Desc,
            SmallVectorImpl<std::optional<LLT>> &TypeVec,
            bool &ConstructionMismatch) {
  unsigned NumTypeIdx = 0;
  for (const MCOperandInfo &OpInfo : Desc.operands())
    if (OpInfo.isGenericType())
      NumTypeIdx = std::max(NumTypeIdx, OpInfo.getGenericTypeIndex() + 1);

  if (A.Types.size() > NumTypeIdx) {
    ConstructionMismatch = true;
    return "corpus has more types than the descriptor's generic type indices";
  }

  TypeVec.assign(NumTypeIdx, std::nullopt);
  for (unsigned I = 0; I != A.Types.size(); ++I)
    TypeVec[I] = A.Types[I];

  for (unsigned I = 0; I != NumTypeIdx; ++I) {
    if (TypeVec[I])
      continue;
    if (A.OpcodeName == "G_BUILD_VECTOR" && I == 1) {
      if (!TypeVec[0] || !TypeVec[0]->isVector())
        return "non-vector build vector";
      TypeVec[1] = TypeVec[0]->getElementType();
      continue;
    }
    return "missing corpus type index " + std::to_string(I) +
           " has no reviewed fill rule";
  }

  for (unsigned I = 0; I != NumTypeIdx; ++I)
    if (!TypeVec[I])
      return "unassigned type index " + std::to_string(I);
  return std::nullopt;
}

Type *fpTypeForWidth(LLVMContext &Ctx, unsigned Bits) {
  switch (Bits) {
  case 16:
    return Type::getHalfTy(Ctx);
  case 32:
    return Type::getFloatTy(Ctx);
  case 64:
    return Type::getDoubleTy(Ctx);
  case 80:
    return Type::getX86_FP80Ty(Ctx);
  case 128:
    return Type::getFP128Ty(Ctx);
  default:
    return nullptr;
  }
}

/// Reviewed immediate table from PRD 4.3 item 5.  Any descriptor operand that
/// is not a generic type must be covered here; otherwise the atom is
/// unbuildable rather than silently given a wrong immediate.
bool hasReviewedImmediate(StringRef Name) {
  return Name == "G_CONSTANT" || Name == "G_FCONSTANT" || Name == "G_ICMP" ||
         Name == "G_FCMP" || Name == "G_EXTRACT" || Name == "G_INSERT" ||
         Name == "G_SEXT_INREG" || Name == "G_IS_FPCLASS" ||
         Name == "G_SHUFFLE_VECTOR";
}

} // namespace

int main(int argc, char **argv) {
  cl::ParseCommandLineOptions(argc, argv, "x86-mir-run\n");
  LLVMInitializeX86TargetInfo();
  LLVMInitializeX86Target();
  LLVMInitializeX86TargetMC();

  const std::string InPath = requireEnv("X86_LEGALIZER_MIR_CORPUS_IN");
  const std::string OutPath = requireEnv("X86_LEGALIZER_MIR_OUT");
  const unsigned long MaxSteps = parseMaxSteps();
  const unsigned long StartIndex = parseStartIndex();
  const Shard Sharding = parseShard();
  const RulesKind Rules = parseRules();

  // Create the output before reading the corpus.  A supervisor timeout during
  // corpus parse then leaves an empty (observable) file instead of looking
  // like a driver that never started.
  std::error_code EC;
  raw_fd_ostream OS(OutPath, EC, sys::fs::OF_Text);
  if (EC)
    infraError(Twine("cannot open output ") + OutPath + ": " + EC.message());

  std::optional<std::string> Text = readFile(InPath);
  if (!Text)
    infraError(Twine("cannot read corpus ") + InPath);

  Expected<json::Value> Parsed = json::parse(*Text);
  if (!Parsed)
    infraError(Twine("invalid corpus JSON: ") + toString(Parsed.takeError()));
  const json::Object *Root = Parsed->getAsObject();
  if (!Root)
    infraError("corpus root is not a JSON object");

  if (Root->getString("schema").value_or("") != "llvm-x86-legalizer-cpp-corpus")
    infraError("corpus schema is not llvm-x86-legalizer-cpp-corpus");

  std::optional<StringRef> ConfigText = Root->getString("config");
  if (!ConfigText)
    infraError("corpus has no config");
  const Config *C = findFullConfig(*ConfigText);
  if (!C)
    infraError(Twine("corpus config is not one of C0-C3: ") + *ConfigText);

  const json::Array *AtomsJson = Root->getArray("atoms");
  if (!AtomsJson)
    infraError("corpus has no atoms array");
  std::optional<int64_t> AtomCountJson = Root->getInteger("atom_count");
  if (!AtomCountJson || *AtomCountJson < 0)
    infraError("corpus has no non-negative atom_count");
  if (static_cast<size_t>(*AtomCountJson) != AtomsJson->size())
    infraError("corpus atom_count does not match the atoms array size");

  Sigma S = makeSigma(*C);
  if (!S.TM || !S.STI)
    infraError(Twine("cannot create subtarget for config ") + C->ID);
  const unsigned PointerBits = S.STI->isTarget64BitLP64() ? 64 : 32;

  // Parse and pre-flight every atom before running any of them.  A malformed
  // type, memory description, or pointer width is an infrastructure error,
  // not a per-atom construction problem.
  std::vector<Atom> Atoms;
  Atoms.reserve(AtomsJson->size());
  for (const json::Value &Value : *AtomsJson) {
    const json::Object *Obj = Value.getAsObject();
    if (!Obj)
      infraError("atom is not a JSON object");
    std::optional<StringRef> ID = Obj->getString("id");
    std::optional<StringRef> Opcode = Obj->getString("opcode");
    const json::Object *Query = Obj->getObject("query");
    if (!ID || !Opcode || !Query)
      infraError("atom is missing id, opcode, or query");
    const json::Array *TypesJson = Query->getArray("types");
    if (!TypesJson)
      infraError(Twine("atom ") + *ID + " has no query.types array");

    Atom A;
    A.ID = ID->str();
    A.OpcodeName = Opcode->str();
    for (const json::Value &TypeValue : *TypesJson) {
      std::optional<StringRef> TypeName = TypeValue.getAsString();
      if (!TypeName)
        infraError(Twine("atom ") + A.ID + " has a non-string type");
      std::optional<LLT> Ty = parseCorpusLLT(*TypeName);
      if (!Ty)
        infraError(Twine("atom ") + A.ID + " has invalid type " + *TypeName);
      if (Ty->isPointer() && Ty->getSizeInBits() != PointerBits)
        infraError(Twine("atom ") + A.ID + " pointer width does not match " +
                   C->ID);
      if (Ty->isPointerVector() &&
          Ty->getElementType().getSizeInBits() != PointerBits)
        infraError(Twine("atom ") + A.ID + " pointer width does not match " +
                   C->ID);
      A.Types.push_back(*Ty);
    }

    if (const json::Object *Mem = Query->getObject("mem")) {
      std::optional<StringRef> MemName = Mem->getString("type");
      std::optional<int64_t> Align = Mem->getInteger("align");
      // A malformed mem operand is a per-atom construction problem (PRD 4.3
      // item 6): report it as an explicit unbuildable outcome later instead of
      // aborting the whole corpus run.  Only pointer-width mismatches remain
      // infrastructure errors.
      if (!MemName) {
        A.UnbuildableReason = "query.mem has no string type";
      } else if (std::optional<LLT> MemTy = parseCorpusLLT(*MemName)) {
        if (MemTy->isPointer() && MemTy->getSizeInBits() != PointerBits)
          infraError(Twine("atom ") + A.ID +
                     " mem pointer width does not match " + C->ID);
        if (!Align || *Align < 1 ||
            !isPowerOf2_64(static_cast<uint64_t>(*Align))) {
          A.UnbuildableReason = "query.mem align is missing or not a "
                                "positive power of two";
        } else {
          A.Mem = MemSpec{*MemTy, static_cast<uint64_t>(*Align)};
        }
      } else {
        A.UnbuildableReason =
            "query.mem type is not a parseable corpus type: " + MemName->str();
      }
    }

    Atoms.push_back(std::move(A));
  }

  const MCInstrInfo &MCII = *S.TM->getMCInstrInfo();
  const X86TargetMachine &TM =
      *static_cast<const X86TargetMachine *>(S.TM.get());
  std::unique_ptr<LegalizerInfo> Reference;
  if (Rules == RulesKind::Reference)
    Reference = std::make_unique<X86LegalizerInfoReference>(*S.STI, TM);
  const LegalizerInfo &LI =
      Rules == RulesKind::Reference ? *Reference : *S.STI->getLegalizerInfo();

  std::map<std::string, unsigned> Counts = {{"legalized", 0},
                                            {"failed", 0},
                                            {"unbuildable", 0},
                                            {"construction_mismatch", 0}};
  std::map<std::string, std::map<std::string, unsigned>> UnbuildableByOpcode;
  std::map<std::string, std::map<std::string, unsigned>> OutcomesByOpcode;

  // Emits an outcome and records the per-opcode partition so the summary can
  // account for every opcode in the shard with either a terminal outcome or an
  // explicit unbuildable reason.
  auto emit = [&](const Atom &A, unsigned Index, const Outcome &O) {
    emitOutcome(OS, A, Index, O);
    ++Counts[O.Kind];
    ++OutcomesByOpcode[A.OpcodeName][O.Kind];
    if (StringRef(O.Kind) == "unbuildable")
      ++UnbuildableByOpcode[A.OpcodeName][O.Detail];
  };

  unsigned AtomCount = 0;
  for (unsigned Index = 0; Index != Atoms.size(); ++Index) {
    if (Index < StartIndex)
      continue;
    if (Index % Sharding.Count != Sharding.Index)
      continue;
    ++AtomCount;

    const Atom &A = Atoms[Index];
    emitMarker(OS, "start", A, Index);

    if (A.UnbuildableReason) {
      emit(A, Index,
           Outcome{"unbuildable", 0, "invalid_mem_operand",
                   *A.UnbuildableReason});
      continue;
    }

    std::optional<unsigned> Opcode = opcodeForName(A.OpcodeName);
    if (!Opcode) {
      emit(A, Index,
           Outcome{"unbuildable", 0, "unknown_opcode",
                   "opcode name is not in the generic opcode set"});
      continue;
    }

    if (std::optional<std::string> Reason =
            fixedUnbuildableReason(A.OpcodeName)) {
      emit(A, Index, Outcome{"unbuildable", 0, "unbuildable_policy", *Reason});
      continue;
    }

    const MCInstrDesc &Desc = MCII.get(*Opcode);
    SmallVector<std::optional<LLT>, 4> TypeVec;
    bool TypeMismatch = false;
    if (std::optional<std::string> Reason =
            assignTypes(A, Desc, TypeVec, TypeMismatch)) {
      emit(A, Index,
           Outcome{TypeMismatch ? "construction_mismatch" : "unbuildable", 0,
                   TypeMismatch ? "type_readback" : "type_assignment_failed",
                   *Reason});
      continue;
    }
    SmallVector<LLT, 4> Types;
    for (const std::optional<LLT> &Ty : TypeVec)
      Types.push_back(*Ty);

    if (std::optional<std::string> Reason =
            checkTypeContract(A.OpcodeName, Types)) {
      emit(A, Index,
           Outcome{"construction_mismatch", 0, "type_contract", *Reason});
      continue;
    }

    // Variadic shape guards (PRD 4.3).
    unsigned NumVariadicUses = 0;
    unsigned NumVariadicDefs = 0;
    unsigned VariadicTypeIdx = 0;
    std::optional<std::string> ShapeReason;
    if (A.OpcodeName == "G_BUILD_VECTOR") {
      if (!Types[0].isVector()) {
        ShapeReason = "non-vector build vector";
      } else {
        NumVariadicUses = Types[0].getNumElements();
        VariadicTypeIdx = 1;
      }
    } else if (A.OpcodeName == "G_CONCAT_VECTORS") {
      if (!Types[0].isVector() || !Types[1].isVector()) {
        ShapeReason = "concat vectors requires vector source and destination";
      } else {
        const unsigned Lanes0 = Types[0].getNumElements();
        const unsigned Lanes1 = Types[1].getNumElements();
        if (Lanes1 == 0 || Lanes0 % Lanes1 != 0)
          ShapeReason = "concat vector lane counts do not divide";
        else
          NumVariadicUses = Lanes0 / Lanes1;
        VariadicTypeIdx = 1;
      }
    } else if (A.OpcodeName == "G_MERGE_VALUES") {
      // The MIR verifier rejects G_MERGE_VALUES on vector operands
      // ("G_MERGE_VALUES cannot operate on vectors"), so only the scalar
      // shape is buildable.
      if (Types[0].isVector() || Types[1].isVector()) {
        ShapeReason = "merge values rejects vector operands";
      } else {
        const unsigned Size0 = Types[0].getSizeInBits().getFixedValue();
        const unsigned Size1 = Types[1].getSizeInBits().getFixedValue();
        if (Size1 == 0 || Size0 % Size1 != 0)
          ShapeReason = "merge value sizes do not divide";
        else
          NumVariadicUses = Size0 / Size1;
        VariadicTypeIdx = 1;
      }
    } else if (A.OpcodeName == "G_UNMERGE_VALUES") {
      const unsigned Size0 = Types[0].getSizeInBits().getFixedValue();
      const unsigned Size1 = Types[1].getSizeInBits().getFixedValue();
      if (Size0 == 0 || Size1 % Size0 != 0) {
        ShapeReason = "unmerge source size does not divide by destination";
      } else {
        NumVariadicDefs = Size1 / Size0;
        VariadicTypeIdx = 0;
      }
    }

    if (ShapeReason) {
      emit(A, Index, Outcome{"unbuildable", 0, "shape_guard", *ShapeReason});
      continue;
    }

    // Immediate-table validity.
    if (A.OpcodeName == "G_CONSTANT" && !Types[0].isScalar()) {
      emit(A, Index,
           Outcome{"unbuildable", 0, "immediate_rule",
                   "G_CONSTANT type is not a scalar integer"});
      continue;
    }
    if (A.OpcodeName == "G_FCONSTANT") {
      LLVMContext ProbeCtx;
      if (!Types[0].isScalar() ||
          !fpTypeForWidth(ProbeCtx, Types[0].getSizeInBits().getFixedValue())) {
        emit(A, Index,
             Outcome{"unbuildable", 0, "immediate_rule",
                     "G_FCONSTANT width has no exact zero encoding"});
        continue;
      }
    }

    // Any non-generic descriptor operand must have a reviewed immediate rule.
    bool HasUnreviewedImmediate = false;
    if (!hasReviewedImmediate(A.OpcodeName))
      for (unsigned OpIdx = 0; OpIdx != Desc.getNumOperands(); ++OpIdx)
        if (!Desc.operands()[OpIdx].isGenericType())
          HasUnreviewedImmediate = true;
    if (HasUnreviewedImmediate) {
      emit(A, Index,
           Outcome{"unbuildable", 0, "immediate_rule",
                   "no reviewed immediate rule for " + A.OpcodeName});
      continue;
    }

    if (A.OpcodeName == "G_SHUFFLE_VECTOR" && !Types[0].isVector()) {
      emit(A, Index,
           Outcome{"unbuildable", 0, "shape_guard",
                   "shuffle vector destination is not a vector"});
      continue;
    }

    // Memory operands are required by the load/store family.
    if (isLoadStoreOpcode(A.OpcodeName) && !A.Mem) {
      emit(A, Index,
           Outcome{"unbuildable", 0, "missing_mem_operand",
                   "load/store atom has no query.mem"});
      continue;
    }

    // Fresh Module/Function/MMI/MF for every atom: MMI caches MFs by
    // Function*, so reusing either would leak instructions and registers.
    LLVMContext Ctx;
    Module M("x86-legalizer-mir", Ctx);
    const Triple TT(C->Triple);
    M.setTargetTriple(TT);
    M.setDataLayout(TT.computeDataLayout());
    Function *F = Function::Create(
        FunctionType::get(Type::getVoidTy(Ctx), false),
        GlobalValue::ExternalLinkage, "atom_" + std::to_string(Index), M);
    MachineModuleInfo MMI(S.TM.get());
    MachineFunction &MF = MMI.getOrCreateMachineFunction(*F);
    MF.getProperties().setIsSSA();
    MachineBasicBlock *MBB = MF.CreateMachineBasicBlock();
    MF.push_back(MBB);

    MachineIRBuilder MIRB;
    MIRB.setMF(MF);
    MIRB.setMBB(*MBB);
    MachineRegisterInfo &MRI = MF.getRegInfo();

    MachineInstrBuilder MIB = MIRB.buildInstrNoInsert(*Opcode);
    SmallVector<std::optional<LLT>, 8> OperandTypes;

    auto addDef = [&](LLT Ty) {
      MIB.addDef(MRI.createGenericVirtualRegister(Ty));
      OperandTypes.push_back(Ty);
    };
    auto addUse = [&](LLT Ty) {
      Register R =
          MIRB.buildInstr(TargetOpcode::G_IMPLICIT_DEF, {DstOp(Ty)}, {})
              .getReg(0);
      MIB.addUse(R);
      OperandTypes.push_back(Ty);
    };
    auto addImmediateOperand = [&](unsigned OpIdx) {
      if (A.OpcodeName == "G_CONSTANT") {
        MIB.addCImm(ConstantInt::get(
            IntegerType::get(Ctx, Types[0].getSizeInBits().getFixedValue()),
            0));
      } else if (A.OpcodeName == "G_FCONSTANT") {
        Type *Ty =
            fpTypeForWidth(Ctx, Types[0].getSizeInBits().getFixedValue());
        MIB.addFPImm(ConstantFP::get(Ty, 0.0));
      } else if (A.OpcodeName == "G_ICMP") {
        MIB.addPredicate(CmpInst::ICMP_EQ);
      } else if (A.OpcodeName == "G_FCMP") {
        MIB.addPredicate(CmpInst::FCMP_FALSE);
      } else if (A.OpcodeName == "G_SEXT_INREG") {
        MIB.addImm(1);
      } else if (A.OpcodeName == "G_SHUFFLE_VECTOR" && OpIdx == 3) {
        SmallVector<int, 8> Mask;
        for (unsigned Lane = 0; Lane != Types[0].getNumElements(); ++Lane)
          Mask.push_back(static_cast<int>(Lane));
        MIB.addShuffleMask(Mask);
      } else {
        MIB.addImm(0);
      }
      OperandTypes.push_back(std::nullopt);
    };

    if (A.OpcodeName == "G_BUILD_VECTOR" ||
        A.OpcodeName == "G_CONCAT_VECTORS" ||
        A.OpcodeName == "G_MERGE_VALUES") {
      addDef(Types[0]);
      for (unsigned I = 0; I != NumVariadicUses; ++I)
        addUse(Types[VariadicTypeIdx]);
    } else if (A.OpcodeName == "G_UNMERGE_VALUES") {
      for (unsigned I = 0; I != NumVariadicDefs; ++I)
        addDef(Types[VariadicTypeIdx]);
      addUse(Types[1]);
    } else {
      for (unsigned OpIdx = 0; OpIdx != Desc.getNumOperands(); ++OpIdx) {
        const MCOperandInfo &OpInfo = Desc.operands()[OpIdx];
        if (OpInfo.isGenericType()) {
          const unsigned TypeIdx = OpInfo.getGenericTypeIndex();
          if (OpIdx < Desc.getNumDefs())
            addDef(Types[TypeIdx]);
          else
            addUse(Types[TypeIdx]);
        } else {
          addImmediateOperand(OpIdx);
        }
      }
    }

    MIRB.insertInstr(MIB);
    MachineInstr *MI = &*MIB;

    // Memory operand: corpus LLT, validated power-of-two alignment, size from
    // the LLT, and load/store flags from the descriptor.
    if (A.Mem) {
      if (!isLoadStoreOpcode(A.OpcodeName)) {
        emit(A, Index,
             Outcome{"construction_mismatch", 0, "shape_guard",
                     "non-load/store atom carries query.mem"});
        continue;
      }
      MachineMemOperand::Flags Flags = MachineMemOperand::MONone;
      if (MI->getDesc().mayLoad())
        Flags |= MachineMemOperand::MOLoad;
      if (MI->getDesc().mayStore())
        Flags |= MachineMemOperand::MOStore;
      const uint64_t Bytes =
          divideCeil(A.Mem->Type.getSizeInBits().getFixedValue(), 8);
      MachineMemOperand *MMO = MF.getMachineMemOperand(
          MachinePointerInfo(), Flags, LocationSize::precise(Bytes),
          Align(A.Mem->Align));
      MIB.addMemOperand(MMO);
    }

    // Liveness anchor: keep the def live so the legalizer actually visits the
    // instruction instead of erasing it as trivially dead.  Def-less ops
    // (G_STORE/G_FPTRUNCSTORE) are already non-trivially-dead via the
    // memoperand and never get an anchor.
    if (MI->getNumDefs() > 0 && isTriviallyDead(*MI, MRI)) {
      Register Def = MI->getOperand(0).getReg();
      LLT DefTy = MRI.getType(Def);
      MIRB.buildInstr(TargetOpcode::COPY, {DstOp(DefTy)}, {SrcOp(Def)});
    }

    // Readback: every register operand must carry the assigned LLT.  Non-reg
    // operands (immediates, shuffle masks) are recorded as nullopt.
    bool ReadbackFailed = false;
    std::string ReadbackDetail;
    if (MI->getNumOperands() != OperandTypes.size()) {
      ReadbackFailed = true;
      ReadbackDetail = "operand count mismatch";
    } else {
      for (unsigned OpIdx = 0; OpIdx != MI->getNumOperands(); ++OpIdx) {
        const MachineOperand &MO = MI->getOperand(OpIdx);
        if (!MO.isReg()) {
          if (OperandTypes[OpIdx])
            ReadbackFailed = true;
        } else if (!OperandTypes[OpIdx] ||
                   MRI.getType(MO.getReg()) != *OperandTypes[OpIdx]) {
          ReadbackFailed = true;
        }
        if (ReadbackFailed) {
          ReadbackDetail =
              "operand " + std::to_string(OpIdx) + " type readback mismatch";
          break;
        }
      }
    }
    if (ReadbackFailed) {
      emit(
          A, Index,
          Outcome{"construction_mismatch", 0, "type_readback", ReadbackDetail});
      continue;
    }

    std::string VerifierText;
    raw_string_ostream VerifierOS(VerifierText);
    if (!MF.verify(nullptr, "constructed MIR", &VerifierOS,
                   /*AbortOnError=*/false)) {
      VerifierOS.flush();
      emit(A, Index,
           Outcome{"construction_mismatch", 0, "verifier",
                   firstLine(VerifierText)});
      continue;
    }

    emitMarker(OS, "constructed", A, Index);
    emitMarker(OS, "legalize", A, Index);

    StepBudgetObserver Budget(A.ID, MaxSteps);
    LostDebugLocObserver LocObserver("legalizer");
    GISelValueTracking VT(MF);
    LegalizerMFResult Result = legalizeMachineFunction(
        MF, LI, {&Budget}, LocObserver, MIRB, /*Libcalls=*/nullptr, &VT);

    if (!Result.FailedOn) {
      emit(A, Index,
           Outcome{"legalized", Budget.getSteps(), "ok",
                   "legalizer terminated"});
      continue;
    }

    // A Libcall action cannot make progress because Libcalls is nullptr.  The
    // legalizer reports that identically to any other failure, so re-query the
    // action on the failed instruction to keep the reason stable.
    const char *DetailCode = "unable_to_legalize";
    std::string Detail = "legalizer could not legalize " + A.OpcodeName;
    if (isPreISelGenericOpcode(Result.FailedOn->getOpcode())) {
      LegalizeActionStep Step = LI.getAction(*Result.FailedOn, MRI);
      if (Step.Action == LegalizeAction::Libcall) {
        DetailCode = "libcall_lowering_disabled";
        Detail = "legalizer requested a libcall but libcall lowering is "
                 "disabled";
      }
    }
    Outcome FailedOn{"failed", Budget.getSteps(), DetailCode, Detail};
    FailedOn.FailedOnOpcode =
        MF.getSubtarget().getInstrInfo()->getName(Result.FailedOn->getOpcode());
    emit(A, Index, FailedOn);
  }

  OS << "{\"event\":\"summary\",\"schema\":"
        "\"llvm-x86-legalizer-mir-atoms\",\"version\":1,\"rules\":\""
     << (Rules == RulesKind::Reference ? "reference" : "production")
     << "\",\"config\":\"" << C->ID << "\",\"atom_count\":" << AtomCount
     << ",\"counts\":{\"legalized\":" << Counts["legalized"]
     << ",\"failed\":" << Counts["failed"]
     << ",\"unbuildable\":" << Counts["unbuildable"]
     << ",\"construction_mismatch\":" << Counts["construction_mismatch"]
     << "},\"outcomes_by_opcode\":{";
  bool FirstOpcode = true;
  for (const auto &[OpcodeName, Kinds] : OutcomesByOpcode) {
    if (!FirstOpcode)
      OS << ",";
    FirstOpcode = false;
    auto CountFor = [&](StringRef Kind) {
      auto It = Kinds.find(Kind.str());
      return It == Kinds.end() ? 0u : It->second;
    };
    OS << "\"" << jsonEscape(OpcodeName)
       << "\":{\"legalized\":" << CountFor("legalized")
       << ",\"failed\":" << CountFor("failed")
       << ",\"unbuildable\":" << CountFor("unbuildable")
       << ",\"construction_mismatch\":" << CountFor("construction_mismatch")
       << "}";
  }
  OS << "},\"unbuildable\":{";
  FirstOpcode = true;
  for (const auto &[OpcodeName, Reasons] : UnbuildableByOpcode) {
    if (!FirstOpcode)
      OS << ",";
    FirstOpcode = false;
    OS << "\"" << jsonEscape(OpcodeName) << "\":\""
       << jsonEscape(Reasons.begin()->first) << "\"";
  }
  OS << "},\"unbuildable_by_opcode\":{";
  FirstOpcode = true;
  for (const auto &[OpcodeName, Reasons] : UnbuildableByOpcode) {
    if (!FirstOpcode)
      OS << ",";
    FirstOpcode = false;
    unsigned Total = 0;
    for (const auto &[Reason, Count] : Reasons)
      Total += Count;
    OS << "\"" << jsonEscape(OpcodeName) << "\":{\"count\":" << Total
       << ",\"reasons\":{";
    bool FirstReason = true;
    for (const auto &[Reason, Count] : Reasons) {
      if (!FirstReason)
        OS << ",";
      FirstReason = false;
      OS << "\"" << jsonEscape(Reason) << "\":" << Count;
    }
    OS << "}}";
  }
  OS << "},\"libcalls\":\"disabled\"}\n";
  OS.flush();

  return 0;
}
