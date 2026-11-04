"""The LLVM GlobalISel legalizer DSL symbol table.

The corpus reader has to understand every method a target's
``*LegalizerInfo.cpp`` constructor can chain, every predicate it can use, the
action vocabulary, and the LLT constructors.  Historically that knowledge was
spread over ``parse.py``/``domain.py`` and grew by "fix the next parse error".

This module makes the API surface explicit and checkable:

* :func:`extract` reads the authoritative headers and returns names, source
  lines, signatures, and hashes.
* :func:`build_table` merges the extracted surface with the curated semantics
  (kind, slot roles, domain support, parser support) into a JSON document.
* :func:`verify` re-extracts and reports drift.  ``parse`` rejects any method
  or predicate that is not in the table.

The table is pinned to the same LLVM revision as the source snapshots.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess

SCHEMA = "mir-corpus-dsl-symbols"
VERSION = 2
LEGALIZER_HEADER = "llvm/include/llvm/CodeGen/GlobalISel/LegalizerInfo.h"
LEGALIZER_SOURCE = "llvm/lib/CodeGen/GlobalISel/LegalizerInfo.cpp"
LOWLEVELTYPE_HEADER = "llvm/include/llvm/CodeGenTypes/LowLevelType.h"
TYPESIZE_HEADER = "llvm/include/llvm/Support/TypeSize.h"
LEGALIZER_HELPER_HEADER = "llvm/include/llvm/CodeGen/GlobalISel/LegalizerHelper.h"
CODEGEN_HEADER = "llvm/include/llvm/Support/CodeGen.h"
X86_SOURCE = "llvm/lib/Target/X86/GISel/X86LegalizerInfo.cpp"
AARCH64_SOURCE = "llvm/lib/Target/AArch64/GISel/AArch64LegalizerInfo.cpp"
ASSET = "dsl_symbols.json"
CHECK_SCHEMA = "mir-corpus-dsl-symbols-check"

# Every file the parser's vocabulary is derived from.  Signatures stay
# authoritative in the headers; the implementations and target sources are
# used for semantics, hashes, and region classification.
_SOURCE_FILES = (
    LEGALIZER_HEADER, LEGALIZER_SOURCE, LOWLEVELTYPE_HEADER, TYPESIZE_HEADER,
    LEGALIZER_HELPER_HEADER, CODEGEN_HEADER, X86_SOURCE, AARCH64_SOURCE,
)
_HEADER_NAMES = _SOURCE_FILES
TARGET_SOURCES = (X86_SOURCE, AARCH64_SOURCE)

# Kinds describe what a chain method does.  ``domain`` is separate: it says
# whether the query generator can model the rule exactly or only record it as
# an opaque coverage gap.
METHOD_KINDS = {
    "actionIf": "action",
    "actionFor": "action",
    "actionForTypeWithAnyImm": "action",
    "actionForCartesianProduct": "action",
    "legalIf": "legal",
    "legalFor": "legal",
    "legalForTypeWithAnyImm": "legal",
    "legalForTypesWithMemDesc": "legal",
    "legalForCartesianProduct": "legal",
    "alwaysLegal": "legal",
    "custom": "custom",
    "customIf": "custom",
    "customFor": "custom",
    "customForCartesianProduct": "custom",
    "lower": "lower",
    "lowerIf": "lower",
    "lowerFor": "lower",
    "lowerForCartesianProduct": "lower",
    "libcall": "libcall",
    "libcallIf": "libcall",
    "libcallFor": "libcall",
    "libcallForCartesianProduct": "libcall",
    "unsupported": "unsupported",
    "unsupportedIf": "unsupported",
    "unsupportedFor": "unsupported",
    "unsupportedIfMemSizeNotPow2": "unsupported",
    "lowerIfMemSizeNotPow2": "mem_rule",
    "lowerIfMemSizeNotByteSizePow2": "mem_rule",
    "bitcastIf": "mutation_rule",
    "widenScalarIf": "mutation_rule",
    "widenScalarFor": "mutation_rule",
    "narrowScalarIf": "mutation_rule",
    "narrowScalarFor": "mutation_rule",
    "moreElementsIf": "mutation_rule",
    "fewerElementsIf": "mutation_rule",
    "widenScalarToNextPow2": "scalar_rule",
    "widenScalarToNextMultipleOf": "scalar_rule",
    "widenScalarOrEltToNextPow2": "scalar_rule",
    "widenScalarOrEltToNextPow2OrMinSize": "scalar_rule",
    "narrowScalar": "scalar_rule",
    "scalarize": "scalar_rule",
    "scalarizeIf": "scalar_rule",
    "minScalarOrElt": "scalar_rule",
    "minScalarOrEltIf": "scalar_rule",
    "widenVectorEltsToVectorMinSize": "scalar_rule",
    "minScalar": "scalar_rule",
    "minScalarIf": "scalar_rule",
    "maxScalarOrElt": "scalar_rule",
    "maxScalar": "scalar_rule",
    "maxScalarIf": "scalar_rule",
    "clampScalar": "scalar_rule",
    "clampScalarOrElt": "scalar_rule",
    "minScalarSameAs": "scalar_rule",
    "maxScalarSameAs": "scalar_rule",
    "scalarSameSizeAs": "scalar_rule",
    "minScalarEltSameAsIf": "scalar_rule",
    "maxScalarEltSameAsIf": "scalar_rule",
    "moreElementsToNextPow2": "element_rule",
    "clampMinNumElements": "element_rule",
    "alignNumElementsTo": "element_rule",
    "clampMaxNumElements": "element_rule",
    "clampNumElements": "element_rule",
    "clampMaxNumElementsStrict": "element_rule",
    "immIdx": "helper",
    "typeIdx": "helper",
    "getAlias": "helper",
}

# ``exact`` means the Python side must be able to model the rule when it shows
# up; ``opaque`` means it is recorded and reported as a coverage gap.  A
# mutation supplied as a lambda is opaque because its target type is computed.
METHOD_DOMAIN = {
    name: "opaque" for name in (
        "bitcastIf", "widenScalarIf", "widenScalarFor", "narrowScalarIf",
        "narrowScalarFor", "moreElementsIf", "fewerElementsIf")
}
METHOD_DOMAIN.update({name: "exact" for name in METHOD_KINDS
                      if name not in METHOD_DOMAIN})

# The parser must be able to record every chain method.  A future method that
# needs new slot semantics starts as ``partial`` and fails the gate.
METHOD_PARSER = {name: "full" for name in METHOD_KINDS}

# Predicates whose evaluation is exact in the Python model.  Unlisted
# predicates are registered but treated as opaque (``None`` match) until the
# domain implements them.
PREDICATE_DOMAIN = {
    name: "exact" for name in (
        "typeIs", "typeIsNot", "typeInSet", "typePairInSet", "typePairIs",
        "typePairAndMemDescInSet", "memTypeIsNot", "memSizeInBytesNotPow2",
        "memSizeNotByteSizePow2", "isScalar", "isVector", "isPointer",
        "isPointerVector", "elementTypeIs", "scalarNarrowerThan",
        "scalarWiderThan", "scalarOrEltNarrowerThan", "scalarOrEltWiderThan",
        "vectorElementCountIsGreaterThan",
        "vectorElementCountIsLessThanOrEqualTo", "typeTupleInSet",
        "atomicOrderingAtLeastOrStrongerThan", "sizeNotMultipleOf",
        "sizeNotPow2", "scalarOrEltSizeNotPow2", "sizeIs", "sameSize",
        "largerThan", "smallerThan", "numElementsNotPow2", "immIs", "immInSet",
        "immIsNot", "all", "any", "predNot")
}
# Atomic ordering needs target memory-model state that the corpus does not
# model; the rule is recorded and reported as a coverage gap.
PREDICATE_DOMAIN["atomicOrderingAtLeastOrStrongerThan"] = "opaque"

ACTION_CARRIES_TYPE = {
    "Legal": False,
    "NarrowScalar": True,
    "WidenScalar": True,
    "FewerElements": True,
    "MoreElements": True,
    "Bitcast": True,
    "Lower": False,
    "Libcall": False,
    "Custom": False,
    "Unsupported": False,
}

METHOD_PARAM_SLOT_NAMES = frozenset({
    "TypeIdx", "TypeIdx0", "TypeIdx1", "SameSizeIdx", "LargeTypeIdx",
    "NarrowTypeIdx", "SmallTypeIdx", "FullIdx", "SubIdx",
})

LLT_SPELLINGS = {
    "scalar": ("s<bits>", "integer"),
    "integer": ("s<bits>", "integer"),
    "pointer": ("p<as>:<bits>", "pointer"),
    "fixed_vector": ("v<lanes><elem>", "vector"),
    "vector": ("v<lanes><elem>", "vector"),
    "scalable_vector": ("nxv<lanes><elem>", "scalable_vector"),
    "float16": ("f16", "float"),
    "float32": ("f32", "float"),
    "float64": ("f64", "float"),
    "float128": ("f128", "float"),
    "bfloat16": ("bf16", "bfloat"),
    "x86fp80": ("f80", "float"),
    "ppcf128": ("ppcf128", "float"),
    "scalarOrVector": ("s|v<lanes><elem>", "union"),
    "token": (None, "token"),
    "floatingPoint": (None, "float"),
    "floatIEEE": (None, "float"),
}


# --- v2 surface: LLT queries, ElementCount, query fields, and friends ------

# ``LLT`` member methods that can appear inside a rule predicate or mutation.
# ``(returns, resolution)``; unlisted extracted methods default to ``ignore``
# with a reason, so the table stays exhaustive without pretending to model
# private helpers.
LLT_QUERY_SEMANTICS = {
    "isValid": ("bool", "exact"),
    "isToken": ("bool", "exact"),
    "isAnyScalar": ("bool", "exact"),
    "isInteger": ("bool", "exact"),
    "isFloat": ("bool", "exact"),
    "isPointer": ("bool", "exact"),
    "isAnyVector": ("bool", "exact"),
    "isIntegerVector": ("bool", "exact"),
    "isFloatVector": ("bool", "exact"),
    "isPointerVector": ("bool", "exact"),
    "isPointerOrPointerVector": ("bool", "exact"),
    "isFloatOrFloatVector": ("bool", "exact"),
    "isScalar": ("bool", "exact"),
    "isVector": ("bool", "exact"),
    "isFloatIEEE": ("bool", "exact"),
    "isFloat16": ("bool", "exact"),
    "isFloat32": ("bool", "exact"),
    "isFloat64": ("bool", "exact"),
    "isFloat128": ("bool", "exact"),
    "isBFloat16": ("bool", "exact"),
    "isX86FP80": ("bool", "exact"),
    "isPPCF128": ("bool", "exact"),
    "getNumElements": ("int", "exact"),
    "getElementCount": ("ec", "exact"),
    "isScalable": ("bool", "exact"),
    "isFixedVector": ("bool", "exact"),
    "isScalableVector": ("bool", "exact"),
    "getSizeInBits": ("typesize", "exact"),
    "getSizeInBytes": ("typesize", "exact"),
    "getScalarType": ("llt", "exact"),
    "getElementType": ("llt", "exact"),
    "getScalarSizeInBits": ("int", "exact"),
    "getAddressSpace": ("int", "exact"),
    "changeVectorElementType": ("llt", "exact"),
    "changeElementType": ("llt", "exact"),
    "changeElementSize": ("llt", "exact"),
    "changeVectorElementCount": ("llt", "exact"),
    "changeElementCount": ("llt", "exact"),
    "divide": ("llt", "exact"),
    "multiplyElements": ("llt", "exact"),
    "isByteSized": ("bool", "exact"),
    "changeToInteger": ("llt", "exact"),
    "isKnownMultipleOf": ("bool", "exact"),
    "getUseExtended": ("bool", "exact"),
    "setUseExtended": ("void", "ignore"),
    "getFpSemantics": ("semantics", "opaque"),
    "getKind": ("kind", "opaque"),
    "print": ("void", "ignore"),
    "dump": ("void", "ignore"),
    "getFieldValue": ("int", "ignore"),
    "getUniqueRAWLLTData": ("int", "ignore"),
    "getMask": ("int", "ignore"),
    "maskAndShift": ("int", "ignore"),
    "init": ("void", "ignore"),
}

# Prefixes that make an LLT method a *query* in the legalizer DSL.  The
# extractor keeps the rest (constructors/operators/private helpers) out of the
# query section.
LLT_QUERY_PREFIXES = ("is", "get", "change", "divide", "multiply", "set",
                      "print", "dump", "toVector", "toScalar")

ELEMENT_COUNT_SEMANTICS = {
    "getFixed": ("constructor", "exact"),
    "getScalable": ("constructor", "exact"),
    "get": ("constructor", "exact"),
    "isScalar": ("bool", "exact"),
    "isVector": ("bool", "exact"),
    "isPowerOf2": ("bool", "exact"),
    "isZero": ("bool", "exact"),
    "isNonZero": ("bool", "exact"),
    "getKnownMinValue": ("int", "exact"),
    "getKnownMinSize": ("int", "exact"),
    "isScalable": ("bool", "exact"),
    "isFixed": ("bool", "exact"),
    "isKnownEven": ("bool", "exact"),
    "isKnownMultipleOf": ("bool", "exact"),
    "divideCoefficientBy": ("ec", "exact"),
    "multiplyCoefficientBy": ("ec", "exact"),
    "coefficientNextPowerOf2": ("ec", "exact"),
    "getFixedValue": ("int", "exact"),
    "getKnownScalarFactor": ("int", "exact"),
    "hasKnownScalarFactor": ("bool", "exact"),
}

# ElementCount inherits from FixedOrScalableQuantity; these names are curated
# but still verified against TypeSize.h by the extractor.
ELEMENT_COUNT_INHERITED = tuple(
    name for name in ELEMENT_COUNT_SEMANTICS
    if name not in ("getFixed", "getScalable", "get"))

QUERY_API_FIELDS = {
    "Opcode": ("opcode", "types_slot"),
    "Types": ("types", "types_slot"),
    "MMODescrs": ("memdesc_list", "mem_slot"),
    "Immediates": ("imm_list", "imm_slot"),
}
QUERY_API_NESTED = {
    "MemDesc": {
        "MemoryTy": ("llt", "mem_slot"),
        "AlignInBits": ("int", "mem_slot"),
        "Ordering": ("atomic_ordering", "mem_slot"),
        "FailureOrdering": ("atomic_ordering", "mem_slot"),
    },
}

LEGALIZER_INFO_API = {
    "getOpcodeIdxForOpcode": ("constructor", "exact", "int"),
    "getActionDefinitionsIdx": ("constructor", "exact", "int"),
    "verify": ("constructor_end", "ignore", "void"),
    "getActionDefinitions": ("runtime_query", "exact", "ruleset"),
    "getActionDefinitionsBuilder": ("constructor", "exact", "ruleset"),
    "aliasActionDefinitions": ("constructor", "exact", "void"),
    "getAction": ("runtime_query", "exact", "action_step"),
    "isLegal": ("runtime_query", "exact", "bool"),
    "isLegalOrCustom": ("runtime_query", "exact", "bool"),
    "legalizeCustom": ("lowering", "ignore", "bool"),
    "legalizeIntrinsic": ("lowering", "ignore", "bool"),
    "getExtOpcodeForWideningConstant": ("runtime_query", "opaque", "int"),
}

# LegalizeHelper is triage-only: it is never part of the parser gate, but the
# action->entrypoint mapping is version-checked so the docs cannot rot.
HELPER_ENTRYPOINTS = {
    "legalizeInstrStep": "dispatch",
    "lower": "Lower",
    "libcall": "Libcall",
    "narrowScalar": "NarrowScalar",
    "widenScalar": "WidenScalar",
    "bitcast": "Bitcast",
    "fewerElementsVector": "FewerElements",
    "moreElementsVector": "MoreElements",
}

TARGET_QUERIES = [
    {"name": "ST.<predicate>", "kind": "subtarget_predicate",
     "binding": "target.features", "scope": "constructor",
     "resolution": "exact"},
    {"name": "ST.getTargetLowering", "kind": "subtarget_query",
     "returns": "TargetLowering", "scope": "constructor",
     "resolution": "exact"},
    {"name": "ST.getInstrInfo", "kind": "subtarget_query",
     "returns": "MCInstrInfo", "scope": "constructor_end",
     "resolution": "ignore"},
    {"name": "TargetLowering.getTargetMachine", "kind": "target_lowering_query",
     "returns": "TargetMachine", "scope": "constructor",
     "resolution": "exact"},
    {"name": "TM.getCodeModel", "kind": "enum_query", "enum": "CodeModel",
     "scope": "constructor", "resolution": "exact"},
]

# C++ syntax the parser must step over rather than interpret.  Reasons are
# reported by `dsl audit` so a human can tell intentional ignores from gaps.
IGNORE_IDENTS = [
    {"name": "static_cast", "reason": "cxx_cast"},
    {"name": "std", "reason": "cxx_namespace"},
    {"name": "LLT", "reason": "cxx_type"},
    {"name": "LegalityQuery", "reason": "cxx_type"},
    {"name": "LegalityPredicate", "reason": "functional_cast"},
    {"name": "LegalizeMutation", "reason": "functional_cast"},
    {"name": "unsigned", "reason": "cxx_type"},
    {"name": "int", "reason": "cxx_type"},
    {"name": "bool", "reason": "cxx_type"},
    {"name": "auto", "reason": "cxx_type"},
    {"name": "const", "reason": "cxx_qualifier"},
]

ENUM_NAMES = ("CodeModel", "LegalizeResult")


# --- extraction ------------------------------------------------------------

def llvm_include_dir(llvm_src):
    """The directory that contains ``llvm/`` from a checkout or llvm dir."""
    path = os.path.realpath(os.path.abspath(llvm_src))
    if os.path.isdir(os.path.join(path, "llvm", "include")):
        return os.path.join(path, "llvm", "include")
    if os.path.isdir(os.path.join(path, "include", "llvm")):
        return os.path.join(path, "include")
    raise FileNotFoundError(f"cannot find llvm/include under {llvm_src}")


def _git_show(repo, revision, path):
    proc = subprocess.run(["git", "-C", repo, "show", f"{revision}:{path}"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git show {path} failed")
    return proc.stdout


def _repo_root(llvm_src):
    """The checkout root that contains ``llvm/``."""
    path = os.path.realpath(os.path.abspath(llvm_src))
    if os.path.isdir(os.path.join(path, "llvm", "include")):
        return path
    if os.path.isdir(os.path.join(path, "include", "llvm")):
        return os.path.dirname(path)
    raise FileNotFoundError(f"cannot find llvm/ under {llvm_src}")


def _read_sources(llvm_src=None, repo=None, revision=None):
    """Read every source file in the v2 surface, pinned or from a checkout."""
    if repo and revision:
        texts = {}
        for name in _SOURCE_FILES:
            try:
                texts[name] = _git_show(repo, revision, name)
            except RuntimeError:
                if name in TARGET_SOURCES:
                    texts[name] = ""
                else:
                    raise
        return texts
    if not llvm_src:
        raise ValueError("either llvm_src or repo+revision is required")
    root = _repo_root(llvm_src)
    texts = {}
    for name in _SOURCE_FILES:
        path = os.path.join(root, name)
        try:
            with open(path, encoding="utf-8") as handle:
                texts[name] = handle.read()
        except OSError:
            if name in TARGET_SOURCES:
                texts[name] = ""
            else:
                raise
    return texts


def _split_params(text):
    params, depth, start = [], 0, 0
    for index, char in enumerate(text):
        if char in "(<[{":
            depth += 1
        elif char in ")>]}":
            depth -= 1
        elif char == "," and depth == 0:
            params.append(text[start:index].strip())
            start = index + 1
    tail = text[start:].strip()
    if tail:
        params.append(tail)
    return params


def _param_name(param):
    match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:=[^,]*)?$", param)
    return match.group(1) if match else ""


def _role(param):
    text = " ".join(param.split())
    if "LegalizeAction" in text:
        return "action"
    if "LegalityPredicate" in text:
        return "predicate"
    if re.search(r"\bbool\s+Pred\b", text):
        return "predicate"
    if "LegalizeMutation" in text:
        return "mutation"
    if "TypePairAndMemDesc" in text:
        return "mem_desc_list" if "initializer_list" in text else "mem_desc"
    if "std::initializer_list<std::tuple" in text:
        return "type_tuple_list"
    if "std::initializer_list<std::pair" in text:
        return "type_pair_list"
    if "std::initializer_list<LLT>" in text:
        return "type_list"
    if re.search(r"\bLLT\b", text):
        return "llt"
    match = re.search(r"\bunsigned\s+([A-Za-z_][A-Za-z0-9_]*)", text)
    if match:
        name = match.group(1)
        if (name in METHOD_PARAM_SLOT_NAMES or name.endswith("TypeIdx")
                or re.fullmatch(r"Type(?:Idx)?\d+", name)):
            return "slot"
        if name in ("ImmIdx", "Imm"):
            return "imm"
        if name in ("MMOIdx", "MemIdx", "MemoryIdx"):
            return "mem"
        if name in ("AddrSpace", "AddressSpace"):
            return "addr_space"
        return "size"
    if "int64_t" in text or "unsigned" in text:
        return "size"
    return "other"


def _match_paren(text, open_index):
    depth = 0
    for index in range(open_index, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _class_body(text, class_name):
    match = re.search(
        rf"(?:class|struct)\s+(?:LLVM_ABI\s+|LLVM_EXPORT\s+)*{class_name}\s*"
        rf"(?:final\s*)?[:\{{]", text)
    if not match:
        raise ValueError(f"cannot find class {class_name}")
    start = text.index("{", match.start())
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return start, index
    raise ValueError(f"unterminated class {class_name}")


_METHOD_RE = re.compile(
    r"(LegalizeRuleSet\s*&\s*|unsigned\s+|LegalizeRule\s*\*?\s+)"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _extract_methods(text):
    start, end = _class_body(text, "LegalizeRuleSet")
    body = text[start:end]
    methods = {}
    for match in _METHOD_RE.finditer(body):
        name = match.group(2)
        if name in ("LegalizeRuleSet", "Set"):
            continue
        open_index = body.index("(", match.start(2))
        close_index = _match_paren(body, open_index)
        if close_index < 0:
            continue
        signature = " ".join(body[match.start():close_index + 1].split())
        params = _split_params(body[open_index + 1:close_index])
        line = text[:start + match.start()].count("\n") + 1
        entry = methods.setdefault(name, {"line": line, "overloads": []})
        entry["overloads"].append({
            "signature": signature,
            "params": [{"name": _param_name(p), "role": _role(p),
                        "decl": " ".join(p.split())} for p in params],
        })
    return methods


_PREDICATE_RE = re.compile(
    r"(?:LLVM_ABI|inline)\s+(?:LegalityPredicate|Predicate)\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _extract_predicates(text):
    predicates = {}
    for match in _PREDICATE_RE.finditer(text):
        name = match.group(1)
        open_index = text.index("(", match.start(1))
        close_index = _match_paren(text, open_index)
        if close_index < 0:
            continue
        params = _split_params(text[open_index + 1:close_index])
        line = text[:match.start()].count("\n") + 1
        entry = predicates.setdefault(name, {"line": line, "overloads": []})
        entry["overloads"].append({
            "signature": " ".join(text[match.start():close_index + 1].split()),
            "params": [{"name": _param_name(p), "role": _role(p),
                        "decl": " ".join(p.split())} for p in params],
        })
    for name in ("all", "any", "predNot"):
        match = re.search(rf"\b{name}\s*\(", text)
        if match:
            predicates.setdefault(name, {
                "line": text[:match.start()].count("\n") + 1,
                "overloads": [{
                    "signature": name,
                    "params": [{"name": "Predicate", "role": "predicate",
                                "decl": "Predicate"}],
                }],
            })
    return predicates


_MUTATION_RE = re.compile(
    r"(?:LLVM_ABI|inline)\s+(?:LegalizeMutation|Mutation)\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _extract_mutations(text):
    match = re.search(r"namespace\s+LegalizeMutations\s*\{", text)
    if not match:
        return {}
    start = text.index("{", match.start())
    depth, end = 0, len(text)
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                end = index
                break
    block = text[start:end]
    mutations = {}
    for entry in _MUTATION_RE.finditer(block):
        name = entry.group(1)
        open_index = block.index("(", entry.start(1))
        close_index = _match_paren(block, open_index)
        if close_index < 0:
            continue
        params = _split_params(block[open_index + 1:close_index])
        line = text[:start + entry.start()].count("\n") + 1
        item = mutations.setdefault(name, {"line": line, "overloads": []})
        item["overloads"].append({
            "signature": " ".join(block[entry.start():close_index + 1].split()),
            "params": [{"name": _param_name(p), "role": _role(p),
                        "decl": " ".join(p.split())} for p in params],
        })
    return mutations


def _extract_actions(text):
    match = re.search(r"enum\s+LegalizeAction[^{]*\{", text)
    if not match:
        raise ValueError("cannot find enum LegalizeAction")
    start = text.index("{", match.start())
    end = text.index("}", start)
    block = text[start + 1:end]
    actions = []
    for line in block.splitlines():
        line = line.split("///")[0].strip()
        if not line or line.startswith("//"):
            continue
        name = line.split(",", 1)[0].strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            actions.append(name)
    return actions


def _extract_llt(text):
    constructors = {}
    pattern = re.compile(r"static\s+(?:constexpr\s+)?LLT\s+"
                         r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")
    for match in pattern.finditer(text):
        name = match.group(1)
        line = text[:match.start()].count("\n") + 1
        constructors.setdefault(name, {"line": line})
    return constructors


_CLASS_METHOD_RE = re.compile(
    r"(?:^|\n)[ \t]*(?:(?:constexpr|static|virtual|inline|explicit|const|"
    r"LLVM_ABI|LLVM_DUMP_METHOD)\s+)*"
    r"([A-Za-z_][A-Za-z0-9_:<>]*)\s*(?:const\s*)?&?\s*"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*\(([^;{}]*)\)\s*(?:const)?\s*"
    r"(?:;|\{|=)")


def _extract_class_methods(text, class_name, prefixes=None, skip=()):
    """Method declarations from one class body, with roles per overload."""
    try:
        start, end = _class_body(text, class_name)
    except ValueError:
        return {}
    body = text[start:end]
    methods = {}
    for match in _CLASS_METHOD_RE.finditer(body):
        name = match.group(2)
        if name == class_name or name in skip or name.startswith("operator"):
            continue
        if prefixes and not name.startswith(prefixes):
            continue
        open_index = body.index("(", match.start(2))
        close_index = _match_paren(body, open_index)
        if close_index < 0:
            continue
        params = _split_params(body[open_index + 1:close_index])
        line = text[:start + match.start(2)].count("\n") + 1
        entry = methods.setdefault(name, {"line": line, "overloads": []})
        entry["overloads"].append({
            "signature": " ".join(
                body[match.start():close_index + 1].split()),
            "params": [{"name": _param_name(p), "role": _role(p),
                        "decl": " ".join(p.split())} for p in params],
        })
    return methods


def _extract_llt_queries(text):
    return _extract_class_methods(text, "LLT", prefixes=LLT_QUERY_PREFIXES)


def _extract_element_count(text):
    methods = _extract_class_methods(text, "ElementCount")
    for name in ELEMENT_COUNT_INHERITED:
        if name in methods:
            continue
        match = re.search(rf"\b{re.escape(name)}\s*\(", text)
        if match:
            methods[name] = {
                "line": text[:match.start()].count("\n") + 1,
                "overloads": [{"signature": name, "params": []}],
                "inherited": True,
            }
    return methods


_FIELD_RE = re.compile(
    r"^[ \t]{2,}((?:const\s+)?[A-Za-z_][A-Za-z0-9_:<>]*)\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*;", re.M)


def _field_kind(type_text):
    text = " ".join(type_text.split())
    if text.startswith("ArrayRef<LLT>"):
        return "types"
    if text.startswith("ArrayRef<MemDesc>"):
        return "memdesc_list"
    if text.startswith("ArrayRef<int64_t>"):
        return "imm_list"
    if text == "LLT":
        return "llt"
    if text == "AtomicOrdering":
        return "atomic_ordering"
    if text in ("unsigned", "uint64_t", "int64_t", "unsigned long"):
        return "int"
    return "other"


_FIELD_LINE_RE = re.compile(
    r"^[ \t]*((?:const\s+)?[A-Za-z_][A-Za-z0-9_:<>]*)\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*;")


def _extract_fields(text, struct_name):
    """Top-level data members only; nested structs are extracted separately."""
    try:
        start, end = _class_body(text, struct_name)
    except ValueError:
        return {}
    body = text[start:end]
    fields = {}
    depth = 0
    offset = start
    for line in body.splitlines(keepends=True):
        before = depth
        depth += line.count("{") - line.count("}")
        if before == 1:
            match = _FIELD_LINE_RE.match(line)
            if match:
                type_text, name = match.group(1), match.group(2)
                fields[name] = {
                    "kind": _field_kind(type_text),
                    "type": " ".join(type_text.split()),
                    "line": text[:offset].count("\n") + 1,
                }
        offset += len(line)
    return fields


def _extract_query_api(text):
    fields = _extract_fields(text, "LegalityQuery")
    nested = _extract_fields(text, "MemDesc")
    return {"fields": fields, "nested": {"MemDesc": nested}}


def _extract_legalizer_info_api(text):
    methods = _extract_class_methods(
        text, "LegalizerInfo",
        prefixes=("get", "is", "alias", "verify", "legalize"))
    return methods


def _extract_helper_entrypoints(text):
    return _extract_class_methods(text, "LegalizerHelper")


def _extract_enum_values(text, enum_name):
    match = re.search(
        rf"enum(?:\s+class)?\s+{enum_name}\s*(?::[^{{]+)?\{{", text)
    if not match:
        return []
    start = text.index("{", match.start())
    end = text.index("}", start)
    block = text[start + 1:end]
    block = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
    block = re.sub(r"//[^\n]*", "", block)
    values = []
    for token in block.split(","):
        name = token.split("=", 1)[0].strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            values.append(name)
    return values


def _extract_code_model(text):
    match = re.search(r"namespace\s+CodeModel\s*\{", text)
    if not match:
        return []
    start = text.index("{", match.start())
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return _extract_enum_values(text[start:index], "Model")
    return []


def _extract_namespaces(texts):
    found = {}
    for path in TARGET_SOURCES:
        text = texts.get(path) or ""
        for match in re.finditer(
                r"using\s+namespace\s+([A-Za-z_][A-Za-z0-9_:]*)\s*;", text):
            name = match.group(1)
            entry = found.setdefault(name, {
                "name": name,
                "line": text[:match.start()].count("\n") + 1,
                "files": [],
            })
            if path not in entry["files"]:
                entry["files"].append(path)
    return found


_FUNC_DEF_RE = re.compile(
    r"^(?:static\s+)?(?:[A-Za-z_][A-Za-z0-9_:<>]*[\s*&]+)+"
    r"([A-Za-z_][A-Za-z0-9_:]*)\s*\([^;{}]*\)\s*(?:const\s*)?\{", re.M)


def _extract_target_functions(texts):
    found = {}
    for path in TARGET_SOURCES:
        text = texts.get(path) or ""
        for match in _FUNC_DEF_RE.finditer(text):
            name = match.group(1)
            found[name] = {
                "name": name,
                "line": text[:match.start(1)].count("\n") + 1,
                "file": path,
                "kind": "member" if "::" in name else "free",
            }
    return found


def extract(llvm_src=None, repo=None, revision=None):
    """Extract the authoritative v2 DSL surface from the pinned sources."""
    texts = _read_sources(llvm_src=llvm_src, repo=repo, revision=revision)
    legalizer = texts[LEGALIZER_HEADER]
    lowlevel = texts[LOWLEVELTYPE_HEADER]
    typesize = texts[TYPESIZE_HEADER]
    helper = texts[LEGALIZER_HELPER_HEADER]
    codegen = texts[CODEGEN_HEADER]
    return {
        # rule-chain DSL
        "methods": _extract_methods(legalizer),
        "predicates": _extract_predicates(legalizer),
        "mutations": _extract_mutations(legalizer),
        "actions": _extract_actions(legalizer),
        "llt": _extract_llt(lowlevel),
        # values used inside predicates and mutations
        "llt_queries": _extract_llt_queries(lowlevel),
        "element_count": _extract_element_count(typesize),
        "query_api": _extract_query_api(legalizer),
        # entry points and triage surface
        "legalizer_info_api": _extract_legalizer_info_api(legalizer),
        "helper_entrypoints": _extract_helper_entrypoints(helper),
        "enums": {
            "CodeModel": _extract_code_model(codegen),
            "LegalizeResult": _extract_enum_values(helper, "LegalizeResult"),
        },
        # target-source vocabulary
        "namespaces": _extract_namespaces(texts),
        "target_functions": _extract_target_functions(texts),
        "source_files": {
            name: hashlib.sha256(texts[name].encode("utf-8")).hexdigest()
            for name in _SOURCE_FILES
        },
    }


# --- table construction ----------------------------------------------------

def _slots(entry):
    names = set()
    for overload in entry["overloads"]:
        for param in overload["params"]:
            if param["role"] == "slot" and param["name"]:
                names.add(param["name"])
    return sorted(names)


def _gated(entry):
    return any(param["role"] == "predicate"
               for overload in entry["overloads"]
               for param in overload["params"])


def _overloads(entry):
    return [{
        "arity": len(overload["params"]),
        "roles": [param["role"] for param in overload["params"]],
        "signature": overload["signature"],
    } for overload in entry["overloads"]]


def build_table(extracted, revision=None):
    """Merge extracted signatures with the curated semantics."""
    methods = []
    for name in sorted(extracted["methods"]):
        entry = extracted["methods"][name]
        methods.append({
            "name": name,
            "kind": METHOD_KINDS.get(name, "unknown"),
            "source": f"LegalizerInfo.h:{entry['line']}",
            "overloads": _overloads(entry),
            "predicate_gated": _gated(entry),
            "slot_args": _slots(entry),
            "domain": METHOD_DOMAIN.get(name, "opaque"),
            "parser": METHOD_PARSER.get(name, "partial"),
        })

    predicates = []
    for name in sorted(extracted["predicates"]):
        entry = extracted["predicates"][name]
        roles = []
        for overload in entry["overloads"]:
            roles.extend(param["role"] for param in overload["params"])
        predicates.append({
            "name": name,
            "kind": "combinator" if name in ("all", "any", "predNot")
                    else "predicate",
            "source": f"LegalizerInfo.h:{entry['line']}",
            "arity": max(len(overload["params"])
                         for overload in entry["overloads"]),
            "overloads": _overloads(entry),
            "roles": sorted(set(roles)),
            "domain": PREDICATE_DOMAIN.get(name, "opaque"),
        })

    mutations = []
    for name in sorted(extracted["mutations"]):
        entry = extracted["mutations"][name]
        mutations.append({
            "name": name,
            "source": f"LegalizerInfo.h:{entry['line']}",
            "arity": max(len(overload["params"])
                         for overload in entry["overloads"]),
            "overloads": _overloads(entry),
        })

    llt = []
    for name in sorted(extracted["llt"]):
        spelling, kind = LLT_SPELLINGS.get(name, (None, "unknown"))
        llt.append({
            "name": name,
            "kind": kind,
            "source": f"LowLevelType.h:{extracted['llt'][name]['line']}",
            "syntax": spelling is not None,
            "corpus_spelling": spelling,
        })

    actions = [{
        "name": name,
        "carries_type": ACTION_CARRIES_TYPE.get(name, False),
    } for name in extracted["actions"]]

    llt_queries = []
    for name in sorted(extracted["llt_queries"]):
        entry = extracted["llt_queries"][name]
        returns, resolution = LLT_QUERY_SEMANTICS.get(
            name, ("other", "ignore"))
        llt_queries.append({
            "name": name,
            "source": f"LowLevelType.h:{entry['line']}",
            "arity": max(len(overload["params"])
                         for overload in entry["overloads"]),
            "returns": returns,
            "resolution": resolution,
            "scope": "lambda",
            "overloads": _overloads(entry),
        })

    element_count = []
    for name in sorted(extracted["element_count"]):
        entry = extracted["element_count"][name]
        kind, resolution = ELEMENT_COUNT_SEMANTICS.get(
            name, ("other", "ignore"))
        element_count.append({
            "name": name,
            "source": f"TypeSize.h:{entry['line']}",
            "kind": kind,
            "resolution": resolution,
            "inherited": bool(entry.get("inherited")),
            "arity": max(len(overload["params"])
                         for overload in entry["overloads"]),
        })

    query_api = []
    for name, info in sorted(extracted["query_api"]["fields"].items()):
        kind, binding = QUERY_API_FIELDS.get(name, (info["kind"], "none"))
        query_api.append({
            "name": name,
            "struct": "LegalityQuery",
            "kind": kind,
            "binding": binding,
            "source": f"LegalizerInfo.h:{info['line']}",
            "resolution": "exact" if name in QUERY_API_FIELDS else "opaque",
        })
    for struct, fields in sorted(extracted["query_api"]["nested"].items()):
        curated = QUERY_API_NESTED.get(struct, {})
        for name, info in sorted(fields.items()):
            kind, binding = curated.get(name, (info["kind"], "none"))
            query_api.append({
                "name": f"{struct}.{name}",
                "struct": struct,
                "kind": kind,
                "binding": binding,
                "source": f"LegalizerInfo.h:{info['line']}",
                "resolution": "exact" if name in curated else "opaque",
            })

    legalizer_info_api = []
    for name in sorted(extracted["legalizer_info_api"]):
        entry = extracted["legalizer_info_api"][name]
        scope, resolution, returns = LEGALIZER_INFO_API.get(
            name, ("runtime_query", "opaque", "other"))
        legalizer_info_api.append({
            "name": name,
            "source": f"LegalizerInfo.h:{entry['line']}",
            "scope": scope,
            "resolution": resolution,
            "returns": returns,
            "arity": max(len(overload["params"])
                         for overload in entry["overloads"]),
        })

    helper_entrypoints = []
    for name in sorted(HELPER_ENTRYPOINTS):
        entry = extracted["helper_entrypoints"].get(name)
        if entry is None:
            continue
        helper_entrypoints.append({
            "name": name,
            "action": HELPER_ENTRYPOINTS[name],
            "source": f"LegalizerHelper.h:{entry['line']}",
            "arity": max(len(overload["params"])
                         for overload in entry["overloads"]),
        })

    enums = [{
        "name": name,
        "values": extracted["enums"].get(name, []),
    } for name in ENUM_NAMES]

    namespaces = [extracted["namespaces"][name]
                  for name in sorted(extracted["namespaces"])]

    target_functions = [
        extracted["target_functions"][name]
        for name in sorted(extracted["target_functions"],
                           key=lambda n: (extracted["target_functions"][n]["file"],
                                          extracted["target_functions"][n]["line"]))
    ]

    return {
        "schema": SCHEMA,
        "version": VERSION,
        "revision": revision,
        "source_files": extracted["source_files"],
        "counts": {
            "methods": len(methods),
            "predicates": len(predicates),
            "mutations": len(mutations),
            "actions": len(actions),
            "llt": len(llt),
            "llt_queries": len(llt_queries),
            "element_count": len(element_count),
            "query_api": len(query_api),
            "legalizer_info_api": len(legalizer_info_api),
            "helper_entrypoints": len(helper_entrypoints),
            "enums": len(enums),
            "namespaces": len(namespaces),
            "target_functions": len(target_functions),
            "ignore": len(IGNORE_IDENTS),
            "target_queries": len(TARGET_QUERIES),
        },
        "methods": methods,
        "predicates": predicates,
        "mutations": mutations,
        "actions": actions,
        "llt_constructors": llt,
        "llt_queries": llt_queries,
        "element_count": element_count,
        "query_api": query_api,
        "legalizer_info_api": legalizer_info_api,
        "helper_entrypoints": helper_entrypoints,
        "enums": enums,
        "namespaces": namespaces,
        "target_functions": target_functions,
        "ignore": [dict(entry) for entry in IGNORE_IDENTS],
        "target_queries": [dict(entry) for entry in TARGET_QUERIES],
    }


# --- runtime access --------------------------------------------------------

def default_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "assets", ASSET)


def load(path=None):
    with open(path or default_path(), encoding="utf-8") as handle:
        doc = json.load(handle)
    if doc.get("schema") != SCHEMA:
        raise ValueError(f"{path or default_path()} is not a {SCHEMA} document")
    return doc


def method_names(table=None):
    return {entry["name"] for entry in (table or load())["methods"]}


def predicate_names(table=None):
    return {entry["name"] for entry in (table or load())["predicates"]}


def mutation_names(table=None):
    return {entry["name"] for entry in (table or load())["mutations"]}


def vocabulary_names(table=None):
    """Every call name the legalizer DSL itself defines."""
    table = table or load()
    return (method_names(table) | predicate_names(table)
            | mutation_names(table) | action_names(table)
            | {entry["name"] for entry in table["llt_constructors"]})


def action_names(table=None):
    return {entry["name"] for entry in (table or load())["actions"]}


def llt_query_entry(name, table=None):
    for entry in (table or load())["llt_queries"]:
        if entry["name"] == name:
            return entry
    return None


def llt_query_names(table=None, resolution=None):
    entries = (table or load())["llt_queries"]
    return {entry["name"] for entry in entries
            if resolution is None or entry["resolution"] == resolution}


def element_count_names(table=None, kind=None):
    entries = (table or load())["element_count"]
    return {entry["name"] for entry in entries
            if kind is None or entry["kind"] == kind}


def query_api_names(table=None, resolution=None):
    entries = (table or load())["query_api"]
    return {entry["name"] for entry in entries
            if resolution is None or entry["resolution"] == resolution}


def legalizer_info_names(table=None, scope=None):
    entries = (table or load())["legalizer_info_api"]
    return {entry["name"] for entry in entries
            if scope is None or entry["scope"] == scope}


def helper_entrypoint_names(table=None):
    return {entry["name"] for entry in (table or load())["helper_entrypoints"]}


def enum_values(name, table=None):
    for entry in (table or load())["enums"]:
        if entry["name"] == name:
            return list(entry["values"])
    return []


def namespace_names(table=None):
    return {entry["name"] for entry in (table or load())["namespaces"]}


def ignore_names(table=None):
    return {entry["name"] for entry in (table or load())["ignore"]}


def target_function_names(table=None):
    return {entry["name"] for entry in (table or load())["target_functions"]}


def method_entry(name, table=None):
    for entry in (table or load())["methods"]:
        if entry["name"] == name:
            return entry
    return None


def is_known_method(name, table=None):
    return method_entry(name, table) is not None


def opaque_methods(table=None):
    return {entry["name"] for entry in (table or load())["methods"]
            if entry["domain"] == "opaque"}


def cartesian_methods(table=None):
    return {entry["name"] for entry in (table or load())["methods"]
            if entry["kind"] in ("action", "legal", "custom", "lower",
                                 "libcall") and "CartesianProduct" in
            entry["name"]}


def type_list_methods(table=None):
    return {entry["name"] for entry in (table or load())["methods"]
            if any(role in ("type_list", "type_pair_list", "type_tuple_list",
                            "mem_desc_list")
                   for overload in entry["overloads"]
                   for role in overload["roles"])}


def slot0_methods(table=None):
    return {entry["name"] for entry in (table or load())["methods"]
            for overload in entry["overloads"]
            if overload["roles"][:1] == ["slot"]}


# --- verification ----------------------------------------------------------

def verify(llvm_src=None, repo=None, revision=None, table=None,
           strict_source=False):
    """Compare the checked-in table with the headers."""
    path = None if table is not None else default_path()
    doc = table or load(path)
    report = {
        "schema": CHECK_SCHEMA,
        "version": VERSION,
        "ok": False,
        "pin": path,
        "revision": doc.get("revision"),
        "counts": doc.get("counts"),
        "checks": [],
        "reasons": [],
        "issues": [],
    }

    def check(name, ok, detail=None):
        entry = {"check": name, "ok": bool(ok)}
        if detail is not None:
            entry["detail"] = detail
        report["checks"].append(entry)
        if not ok:
            report["reasons"].append(name)
        return ok

    try:
        extracted = extract(llvm_src=llvm_src, repo=repo, revision=revision)
    except (OSError, RuntimeError, ValueError) as err:
        check("extract", False, str(err))
        return report

    required = ("methods", "predicates", "mutations", "actions",
                "llt_constructors", "llt_queries", "element_count",
                "query_api", "legalizer_info_api", "helper_entrypoints",
                "enums", "namespaces", "target_functions", "ignore",
                "target_queries")
    missing_sections = [key for key in required if key not in doc]
    check("table-sections", not missing_sections, missing_sections)
    check("table-version", int(doc.get("version") or 0) >= 2,
          {"table": doc.get("version"), "expected": VERSION})
    if missing_sections:
        report["ok"] = False
        return report

    sections = (
        ("methods", "methods"),
        ("predicates", "predicates"),
        ("mutations", "mutations"),
        ("llt_queries", "llt_queries"),
        ("element_count", "element_count"),
        ("legalizer_info_api", "legalizer_info_api"),
        ("target_functions", "target_functions"),
        ("namespaces", "namespaces"),
    )
    for table_key, extracted_key in sections:
        expected = {entry["name"] for entry in doc[table_key]}
        actual = set(extracted[extracted_key])
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        check(f"{table_key}-names", not missing and not extra,
              {"missing": missing, "extra": extra})
        for name in missing:
            report["issues"].append({"kind": table_key, "issue": "missing",
                                     "name": name})
        for name in extra:
            report["issues"].append({"kind": table_key,
                                     "issue": "unregistered", "name": name})

    query_actual = set(extracted["query_api"]["fields"])
    for struct, fields in extracted["query_api"]["nested"].items():
        query_actual |= {f"{struct}.{name}" for name in fields}
    query_expected = {entry["name"] for entry in doc["query_api"]}
    check("query_api-names", query_expected == query_actual,
          {"missing": sorted(query_expected - query_actual),
           "extra": sorted(query_actual - query_expected)})

    helper_expected = {entry["name"] for entry in doc["helper_entrypoints"]}
    helper_actual = set(extracted["helper_entrypoints"])
    missing_helpers = sorted(helper_expected - helper_actual)
    check("helper_entrypoints-names", not missing_helpers,
          {"missing": missing_helpers})

    enum_expected = {entry["name"]: entry["values"] for entry in doc["enums"]}
    enum_actual = {name: extracted["enums"].get(name, [])
                   for name in enum_expected}
    check("enum-values", enum_expected == enum_actual,
          {"missing": {name: [value for value in values
                              if value not in enum_actual.get(name, [])]
                       for name, values in enum_expected.items()
                       if values != enum_actual.get(name)}})

    check("actions", [entry["name"] for entry in doc["actions"]]
          == list(extracted["actions"]),
          {"table": [entry["name"] for entry in doc["actions"]],
           "headers": list(extracted["actions"])})

    expected_llt = {entry["name"] for entry in doc["llt_constructors"]}
    actual_llt = set(extracted["llt"])
    check("llt-names", expected_llt == actual_llt,
          {"missing": sorted(expected_llt - actual_llt),
           "extra": sorted(actual_llt - expected_llt)})

    unknown = [entry["name"] for entry in doc["methods"]
               if entry["kind"] == "unknown"]
    check("method-kinds", not unknown, unknown)
    unparsed = [entry["name"] for entry in doc["methods"]
                if entry["parser"] == "none"]
    check("parser-support", not unparsed, unparsed)

    unresolved = [entry["name"] for entry in doc["llt_queries"]
                  if entry["resolution"] not in ("exact", "opaque", "ignore")]
    check("llt-query-resolution", not unresolved, unresolved)

    drift = []
    for name, digest in extracted["source_files"].items():
        recorded = (doc.get("source_files") or {}).get(name)
        if recorded and recorded != digest:
            drift.append({"file": name, "recorded": recorded,
                          "current": digest})
    check("source-hashes", not drift or not strict_source, drift)

    report["ok"] = bool(report["checks"]) and all(
        entry["ok"] for entry in report["checks"])
    return report
