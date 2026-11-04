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
VERSION = 1
LEGALIZER_HEADER = "llvm/include/llvm/CodeGen/GlobalISel/LegalizerInfo.h"
LOWLEVELTYPE_HEADER = "llvm/include/llvm/CodeGenTypes/LowLevelType.h"
ASSET = "dsl_symbols.json"
CHECK_SCHEMA = "mir-corpus-dsl-symbols-check"

_HEADER_NAMES = (LEGALIZER_HEADER, LOWLEVELTYPE_HEADER)

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


def _read_headers(llvm_src=None, repo=None, revision=None):
    if repo and revision:
        return {name: _git_show(repo, revision, name) for name in _HEADER_NAMES}
    if not llvm_src:
        raise ValueError("either llvm_src or repo+revision is required")
    root = os.path.dirname(llvm_include_dir(llvm_src))
    return {name: open(os.path.join(root, name), encoding="utf-8").read()
            for name in _HEADER_NAMES}


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
    match = re.search(rf"class\s+{class_name}\s*\{{", text)
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


def extract(llvm_src=None, repo=None, revision=None):
    """Extract the authoritative DSL surface from the LLVM headers."""
    texts = _read_headers(llvm_src=llvm_src, repo=repo, revision=revision)
    legalizer = texts[LEGALIZER_HEADER]
    lowlevel = texts[LOWLEVELTYPE_HEADER]
    return {
        "methods": _extract_methods(legalizer),
        "predicates": _extract_predicates(legalizer),
        "mutations": _extract_mutations(legalizer),
        "actions": _extract_actions(legalizer),
        "llt": _extract_llt(lowlevel),
        "source_files": {
            name: hashlib.sha256(texts[name].encode("utf-8")).hexdigest()
            for name in _HEADER_NAMES
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


def build_table(extracted, revision=None):
    """Merge extracted signatures with the curated semantics."""
    methods = []
    for name in sorted(extracted["methods"]):
        entry = extracted["methods"][name]
        methods.append({
            "name": name,
            "kind": METHOD_KINDS.get(name, "unknown"),
            "source": f"LegalizerInfo.h:{entry['line']}",
            "overloads": [{
                "arity": len(overload["params"]),
                "roles": [param["role"] for param in overload["params"]],
                "signature": overload["signature"],
            } for overload in entry["overloads"]],
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
            "overloads": [{
                "arity": len(overload["params"]),
                "roles": [param["role"] for param in overload["params"]],
                "signature": overload["signature"],
            } for overload in entry["overloads"]],
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
            "overloads": [{
                "arity": len(overload["params"]),
                "roles": [param["role"] for param in overload["params"]],
                "signature": overload["signature"],
            } for overload in entry["overloads"]],
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
        },
        "methods": methods,
        "predicates": predicates,
        "mutations": mutations,
        "actions": actions,
        "llt_constructors": llt,
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

    for kind in ("methods", "predicates"):
        expected = {entry["name"] for entry in doc[kind]}
        actual = set(extracted[kind])
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        check(f"{kind}-names", not missing and not extra,
              {"missing": missing, "extra": extra})
        for name in missing:
            report["issues"].append({"kind": kind, "issue": "missing",
                                     "name": name})
        for name in extra:
            report["issues"].append({"kind": kind, "issue": "unregistered",
                                     "name": name})

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
