//===-- CorpusOracle.cpp ----------------------------------------*- C++ -*-===//
//
// Out-of-tree oracle filler: read the legality points produced by
// `x86-mir-corpus corpus` and answer each query with the frozen
// X86LegalizerInfoReference.
//
//   X86_LEGALIZER_CPP_POINTS=<points.json> \
//   X86_LEGALIZER_CPP_CORPUS_OUT=<corpus.json> \
//   x86-mir-oracle
//
// The points file is the only input: it names the opcode, the type tuple and
// the optional memory descriptor.  Nothing here reads the .td or its generated
// .inc; the answer comes from X86LegalizerInfoReference, which is the frozen
// oracle.  Points are consumed in file order, which the Python generator
// sorts, so the output is deterministic.
//
// Extracted from
// llvm/unittests/Target/X86/X86LegalizerInfoDifferentialTest.cpp
// (GenerateCorpusFromCppPoints) at llvm-project 8bc4cd96f4bb.
//
// Infrastructure problems print "X86_LEGALIZER_CPP_INFRA_ERROR: " on stderr
// and exit 2; malformed input is never silently skipped.
//
//===----------------------------------------------------------------------===//

#include "X86LegalizerInfoReference.h"
#include "X86LegalizerTestSupport.h"
#include "X86Subtarget.h"
#include "X86TargetMachine.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/ADT/StringExtras.h"
#include "llvm/ADT/StringRef.h"
#include "llvm/ADT/StringSwitch.h"
#include "llvm/CodeGen/GlobalISel/LegalizerInfo.h"
#include "llvm/CodeGen/TargetOpcodes.h"
#include "llvm/Support/Error.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/SHA256.h"
#include "llvm/Support/TargetSelect.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/TargetParser/Triple.h"
#include <cstdlib>
#include <optional>
#include <string>
#include <utility>

using namespace llvm;
using namespace llvm::x86legalizer;

namespace {

[[noreturn]] void infraError(const Twine &Msg) {
  errs() << "X86_LEGALIZER_CPP_INFRA_ERROR: " << Msg << "\n";
  errs().flush();
  std::exit(2);
}

std::string requireEnv(const char *Name) {
  const char *Value = std::getenv(Name);
  if (!Value || !*Value)
    infraError(Twine("missing environment variable ") + Name);
  return Value;
}

std::string sha256Hex(StringRef Data) {
  return toHex(SHA256::hash(arrayRefFromStringRef(Data)), /*LowerCase=*/true);
}

// The 63 table descriptors only name the opcodes the .td test tables cover;
// a C++-derived corpus also queries the other opcodes a builder mentions.
// Build the full mapping from the same .def the enum comes from.
std::optional<unsigned> opcodeForAnyName(StringRef Name) {
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

std::optional<LLT> parseJSONLLT(StringRef S, unsigned PointerBits) {
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

// A step's new type can be a pointer vector (G_IMPLICIT_DEF clamps one), so
// it is spelled with the same full-fidelity LLT names as a query.
std::string witnessLLTName(LLT T) {
  std::string S;
  raw_string_ostream OS(S);
  if (T.isPointer()) {
    OS << "p" << T.getAddressSpace() << ":" << T.getSizeInBits();
  } else if (T.isPointerVector()) {
    OS << "v" << T.getNumElements() << "p"
       << T.getElementType().getAddressSpace() << ":"
       << T.getElementType().getSizeInBits();
  } else if (T.isVector()) {
    OS << "v" << T.getNumElements() << "s" << T.getScalarSizeInBits();
  } else {
    OS << "s" << T.getSizeInBits();
  }
  return OS.str();
}

// LegalizeAction::Lower and Libcall carry no type argument; treating their
// (unused) NewType as a decision field would compare uninitialized data.
bool actionCarriesType(LegalizeAction Action) {
  switch (Action) {
  case LegalizeActions::Legal:
  case LegalizeActions::Unsupported:
  case LegalizeActions::Custom:
  case LegalizeActions::Lower:
  case LegalizeActions::Libcall:
    return false;
  default:
    return true;
  }
}

void renderStepJson(raw_ostream &OS, const LegalizeActionStep &S) {
  OS << "{\"action\":\"" << S.Action << "\"";
  if (actionCarriesType(S.Action))
    OS << ",\"idx\":" << S.TypeIdx << ",\"to\":\"" << witnessLLTName(S.NewType)
       << "\"";
  OS << "}";
}

// The feature map a points file must carry for *STI*.  Kept in one place so
// the filler and the config validator cannot drift.
SmallVector<std::pair<StringRef, bool>, 18>
configFeatures(const X86Subtarget &STI) {
  return {{"is64Bit", STI.is64Bit()},
          {"canUseCMOV", STI.canUseCMOV()},
          {"hasSSE1", STI.hasSSE1()},
          {"hasSSE2", STI.hasSSE2()},
          {"hasSSE41", STI.hasSSE41()},
          {"hasAVX", STI.hasAVX()},
          {"hasAVX2", STI.hasAVX2()},
          {"hasAVX512", STI.hasAVX512()},
          {"hasVLX", STI.hasVLX()},
          {"hasDQI", STI.hasDQI()},
          {"hasBWI", STI.hasBWI()},
          {"useSoftFloat", STI.useSoftFloat()},
          {"hasX87", STI.hasX87()},
          {"hasPOPCNT", STI.hasPOPCNT()},
          {"hasLZCNT", STI.hasLZCNT()},
          {"hasBMI", STI.hasBMI()},
          {"hasVPOPCNTDQ", STI.hasVPOPCNTDQ()},
          {"isTarget64BitLP64", STI.isTarget64BitLP64()}};
}

std::optional<std::string>
validateConfigDescriptor(const json::Object &Desc, const Config &C,
                         const X86Subtarget &STI) {
  std::optional<StringRef> DescId = Desc.getString("id");
  if (!DescId)
    return "config_descriptor has no id";
  if (*DescId != C.ID)
    return std::string("config descriptor id does not match config ") + C.ID;
  const json::Object *Features = Desc.getObject("features");
  if (!Features)
    return "config_descriptor has no features";
  for (const auto &[Name, Actual] : configFeatures(STI)) {
    std::optional<bool> Expected = Features->getBoolean(Name);
    if (!Expected)
      return std::string("config_descriptor is missing feature ") + Name.str();
    if (*Expected != Actual)
      return std::string("config ") + C.ID + " feature " + Name.str() +
             " mismatch";
  }
  return std::nullopt;
}

} // namespace

int main() {
  LLVMInitializeX86TargetInfo();
  LLVMInitializeX86Target();
  LLVMInitializeX86TargetMC();

  const std::string PointsPath = requireEnv("X86_LEGALIZER_CPP_POINTS");
  const std::string OutPath = requireEnv("X86_LEGALIZER_CPP_CORPUS_OUT");

  std::optional<std::string> Text = readFile(PointsPath);
  if (!Text)
    infraError(Twine("cannot read ") + PointsPath);

  Expected<json::Value> Parsed = json::parse(*Text);
  if (!Parsed)
    infraError(Twine("invalid points JSON: ") + toString(Parsed.takeError()));
  const json::Object *Root = Parsed->getAsObject();
  if (!Root)
    infraError("points root is not a JSON object");

  std::optional<StringRef> ConfigOpt = Root->getString("config");
  if (!ConfigOpt)
    infraError("points file has no config");
  const Config *C = findFullConfig(*ConfigOpt);
  if (!C)
    infraError(Twine("unknown full config ") + *ConfigOpt);
  Sigma S = makeSigma(*C);
  if (!S.TM || !S.STI)
    infraError(Twine("cannot create subtarget for config ") + C->ID);

  // The points file carries the subtarget feature map it was generated for.
  // A bare config ID is not enough: the ID once selected a sigma table while
  // the filler used a full config, which hid eight wrong answers.
  const json::Object *Desc = Root->getObject("config_descriptor");
  if (!Desc)
    infraError("points file has no config_descriptor");
  if (std::optional<std::string> Error =
          validateConfigDescriptor(*Desc, *C, *S.STI))
    infraError(*Error);

  X86LegalizerInfoReference Oracle(
      *S.STI, *static_cast<const X86TargetMachine *>(S.TM.get()));
  const unsigned PointerBits = C->Is64 ? 64 : 32;

  const json::Array *Points = Root->getArray("points");
  if (!Points)
    infraError("points file has no points array");

  StringRef SourceSha;
  if (const json::Object *Source = Root->getObject("source"))
    if (std::optional<StringRef> Sha = Source->getString("sha256"))
      SourceSha = *Sha;

  std::string Out;
  raw_string_ostream OS(Out);
  auto EmitField = [&](StringRef Name, const json::Value *Value) {
    OS << "  \"" << Name << "\": ";
    if (Value)
      OS << *Value;
    else
      OS << "null";
    OS << ",\n";
  };
  OS << "{\n  \"schema\": \"llvm-x86-legalizer-cpp-corpus\",\n";
  OS << "  \"version\": 2,\n";
  EmitField("source", Root->get("source"));
  EmitField("oracle", Root->get("oracle"));
  EmitField("config_descriptor", Root->get("config_descriptor"));
  EmitField("coverage", Root->get("coverage"));
  EmitField("counts", Root->get("counts"));
  OS << "  \"source_sha256\": \"" << SourceSha << "\",\n";
  OS << "  \"config\": \"" << C->ID << "\",\n";
  OS << "  \"points_sha256\": \"" << sha256Hex(*Text) << "\",\n";
  OS << "  \"atom_count\": " << Points->size() << ",\n";
  OS << "  \"atoms\": [\n";

  unsigned Emitted = 0;
  for (const json::Value &V : *Points) {
    const json::Object *P = V.getAsObject();
    if (!P)
      infraError(Twine("point ") + Twine(Emitted) + " is not an object");
    std::optional<StringRef> IdOpt = P->getString("id");
    std::optional<StringRef> OriginOpt = P->getString("origin");
    std::optional<StringRef> RuleSetOpt = P->getString("rule_set");
    std::optional<StringRef> OpcodeOpt = P->getString("opcode");
    if (!IdOpt || !OriginOpt || !RuleSetOpt || !OpcodeOpt)
      infraError(Twine("point ") + Twine(Emitted) +
                 " is missing id, origin, rule_set, or opcode");
    StringRef Id = *IdOpt, Origin = *OriginOpt, RuleSet = *RuleSetOpt,
              Opcode = *OpcodeOpt;
    std::optional<unsigned> Op = opcodeForAnyName(Opcode);
    if (!Op)
      infraError(Twine("point ") + Id + " has unknown opcode " + Opcode);

    const json::Array *Types = P->getArray("types");
    if (!Types)
      infraError(Twine("point ") + Id + " has no types array");
    SmallVector<LLT, 3> TypeVec;
    for (const json::Value &T : *Types) {
      std::optional<StringRef> Name = T.getAsString();
      if (!Name)
        infraError(Twine("point ") + Id + " has a non-string type");
      std::optional<LLT> Ty = parseJSONLLT(*Name, PointerBits);
      if (!Ty)
        infraError(Twine("point ") + Id + " has invalid type " + *Name);
      TypeVec.push_back(*Ty);
    }

    SmallVector<LegalityQuery::MemDesc, 1> MemDescs;
    uint64_t Align = 0;
    if (const json::Value *MemValue = P->get("mem")) {
      if (!MemValue->getAsNull()) {
        std::optional<StringRef> Name = MemValue->getAsString();
        if (!Name)
          infraError(Twine("point ") + Id + " has a non-string mem type");
        std::optional<LLT> MemTy = parseJSONLLT(*Name, PointerBits);
        if (!MemTy)
          infraError(Twine("point ") + Id + " has invalid mem type " + *Name);
        std::optional<int64_t> A = P->getInteger("align");
        if (!A)
          infraError(Twine("point ") + Id + " has mem but no align");
        Align = static_cast<uint64_t>(*A);
        MemDescs.emplace_back(*MemTy, Align, AtomicOrdering::NotAtomic,
                              AtomicOrdering::NotAtomic);
      }
    }

    LegalityQuery Q(*Op, TypeVec, MemDescs);
    const LegalizeActionStep Step = Oracle.getAction(Q);

    std::optional<int64_t> RuleIndex = P->getInteger("rule_index");
    OS << "    {\"id\":\"" << Id << "\",\"origin\":\"" << Origin
       << "\",\"rule_set\":\"" << RuleSet << "\",\"opcode\":\"" << Opcode
       << "\",\"rule_index\":";
    if (RuleIndex)
      OS << *RuleIndex;
    else
      OS << "null";
    OS << ",\"config\":\"" << C->ID << "\",\"query\":{\"types\":[";
    for (unsigned I = 0; I != TypeVec.size(); ++I)
      OS << (I ? "," : "") << "\"" << witnessLLTName(TypeVec[I]) << "\"";
    OS << "]";
    if (!MemDescs.empty())
      OS << ",\"mem\":{\"type\":\"" << witnessLLTName(MemDescs[0].MemoryTy)
         << "\",\"align\":" << MemDescs[0].AlignInBits << "}";
    OS << "},\"expected\":";
    renderStepJson(OS, Step);
    OS << "}" << (++Emitted == Points->size() ? "\n" : ",\n");
  }
  OS << "  ]\n}\n";
  OS.flush();

  std::error_code EC;
  raw_fd_ostream File(OutPath, EC);
  if (EC)
    infraError(Twine("cannot open output ") + OutPath + ": " + EC.message());
  File << Out;
  File.close();
  if (File.has_error())
    infraError(Twine("cannot write output ") + OutPath);
  outs() << "wrote " << Emitted << " atoms to " << OutPath << "\n";
  return 0;
}
