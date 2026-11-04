"""Region-aware audit of a target legalizer source against the symbol table.

The parser only sees the constructor chain.  The audit sees the whole file and
answers a different question: for every identifier that looks like a DSL call,
in which scope does it appear, and does the symbol table classify it?

Scopes are derived structurally, not by scanning names:

* ``constructor`` -- the body of ``<Target>LegalizerInfo::<Target>LegalizerInfo``
* ``lambda``      -- a lambda body nested inside the constructor
* ``lowering``    -- any other member function body (custom lowering, helpers)
* ``file``        -- declarations, namespaces, includes

Only ``constructor`` and ``lambda`` occurrences can fail ``--strict``.  The
lowering surface is reported, but it is intentionally outside the parser gate.
"""

import hashlib
import json
import os
import re
import subprocess
from collections import Counter

from . import dsl as _dsl
from . import pin as _pin
from .lexer import lex, matching

SCHEMA = "x86-mir-corpus-legalizer-surface"
VERSION = 1

# C++ syntax that shows up as a call/member but carries no DSL meaning.
_CPP_IGNORE = {
    "if", "for", "while", "switch", "return", "sizeof", "decltype", "alignof",
    "static_cast", "reinterpret_cast", "const_cast", "dynamic_cast",
    "make_pair", "move", "forward", "assert", "min", "max", "abs",
    "make_optional", "optional", "nullptr", "NULL", "this",
}

_CLASS_RE = re.compile(r"\b(\w+LegalizerInfo)::\1\s*\(")


def _entry_index(table):
    """name -> (resolution, section) for every symbol-table section."""
    index = {}

    def add(section, entry, resolution):
        name = entry.get("name")
        if not name:
            return
        index.setdefault(name, (resolution, section))

    for entry in table["methods"]:
        add("method", entry, entry.get("domain", "opaque"))
    for entry in table["predicates"]:
        add("predicate", entry, entry.get("domain", "opaque"))
    for entry in table["mutations"]:
        add("mutation", entry, "exact")
    for entry in table["actions"]:
        add("action", entry, "exact")
    for entry in table["llt_constructors"]:
        add("llt_constructor", entry, entry.get("resolution", "exact"))
    for entry in table["llt_queries"]:
        add("llt_query", entry, entry.get("resolution", "exact"))
    for entry in table["element_count"]:
        add("element_count", entry, entry.get("resolution", "exact"))
    for entry in table["query_api"]:
        add("query_api", entry, entry.get("resolution", "exact"))
    for entry in table["legalizer_info_api"]:
        add("legalizer_info_api", entry, entry.get("resolution", "exact"))
    for entry in table["helper_entrypoints"]:
        add("helper_entrypoint", entry, "ignore")
    for entry in table["namespaces"]:
        add("namespace", entry, "ignore")
    for entry in table["ignore"]:
        add("ignore", entry, "ignore")
    for entry in table["enums"]:
        add("enum", entry, "ignore")
    return index


def _section_names(table):
    return {
        "mutation": {e["name"] for e in table["mutations"]},
        "predicate": {e["name"] for e in table["predicates"]},
        "action": {e["name"] for e in table["actions"]},
        "llt_constructor": {e["name"] for e in table["llt_constructors"]},
        "llt_query": {e["name"] for e in table["llt_queries"]},
        "element_count": {e["name"] for e in table["element_count"]},
    }


def _function_scopes(toks, class_name):
    """Body ranges of every ``Class::method`` definition in the file."""
    scopes = []
    definitions = set()
    i = 0
    while i + 3 < len(toks):
        if (toks[i].kind == "id" and toks[i + 1].text == "::"
                and toks[i + 2].kind == "id" and toks[i + 3].text == "("):
            close = matching(toks, i + 3)
            if close > 0:
                k = close + 1
                while k < len(toks) and toks[k].text not in ("{", ";"):
                    k += 1
                if k < len(toks) and toks[k].text == "{":
                    end = matching(toks, k)
                    if end > 0:
                        name = toks[i + 2].text
                        scopes.append({
                            "name": name,
                            "kind": ("constructor"
                                     if toks[i].text == class_name
                                     and name == class_name else "lowering"),
                            "start": k + 1,
                            "end": end,
                        })
                        definitions.add(i + 2)
                        i = end + 1
                        continue
        i += 1
    return scopes, definitions


def _free_function_scopes(toks, class_funcs):
    """Bodies of namespace-scope helpers (``matchLDPSTPAddrMode`` etc.)."""
    occupied = [(f["start"], f["end"]) for f in class_funcs]
    scopes = []
    i = 0
    while i + 1 < len(toks):
        if (toks[i].kind == "id" and toks[i + 1].text == "("
                and (i == 0 or toks[i - 1].text not in (".", "->", "::"))
                and toks[i].text not in _CPP_IGNORE
                and toks[i].text not in _DECL_KEYWORDS
                and toks[i].text not in _SUBTARGETS):
            close = matching(toks, i + 1)
            if close > 0:
                k = close + 1
                while k < len(toks) and toks[k].text not in ("{", ";"):
                    k += 1
                if k < len(toks) and toks[k].text == "{":
                    end = matching(toks, k)
                    if end > 0 and not any(
                            lo <= i < hi for lo, hi in occupied):
                        scopes.append({"name": toks[i].text,
                                       "kind": "lowering",
                                       "start": k + 1, "end": end})
                        i = end + 1
                        continue
        i += 1
    return scopes


def _lambda_scopes(toks, lo, hi):
    scopes = []
    i = lo
    while i < hi:
        if toks[i].text == "[":
            close = matching(toks, i)
            if close > 0:
                k = close + 1
                if k < hi and toks[k].text == "(":
                    args = matching(toks, k)
                    if args > 0:
                        k = args + 1
                if k < hi and toks[k].text == "{":
                    end = matching(toks, k)
                    if end > 0 and end <= hi:
                        scopes.append({"start": k + 1, "end": end})
                        i = end + 1
                        continue
        i += 1
    return scopes


def _scope_at(pos, funcs, lambdas):
    for lam in lambdas:
        if lam["start"] <= pos < lam["end"]:
            return "lambda"
    for func in funcs:
        if func["start"] <= pos < func["end"]:
            return func["kind"]
    return "file"


def _class_name(text):
    match = _CLASS_RE.search(text)
    return match.group(1) if match else None


def _receiver(toks, i):
    """(receiver, qualifier) for a member/qualified call at *i*."""
    if i >= 1 and toks[i - 1].text in (".", "->"):
        return (toks[i - 2].text if i >= 2 else None, toks[i - 1].text)
    if i >= 2 and toks[i - 1].text == "::":
        receiver = toks[i - 2].text
        # ``::any(...)`` is global qualification, not a member of ``(``.
        if receiver in ("(", ",", "=", "return", "&&", "||"):
            return (None, None)
        return (receiver, "::")
    return (None, None)


_QUERY_FIELDS = {"Types", "Opcode", "MMODescrs", "Immediates",
                 "MemoryTy", "AlignInBits", "Ordering", "FailureOrdering"}
_TARGET_RESULTS = {
    "getTargetMachine": "exact",
    "getTargetLowering": "exact",
    "getInstrInfo": "ignore",
    "getCodeModel": "exact",
}
_SUBTARGETS = ("ST", "STI", "Subtarget", "Subtgt")
_TYPE_NAMESPACES = {
    "LLT": "llt_constructor",
    "ElementCount": "element_count",
    "LegalizeMutations": "mutation",
    "LegalityPredicates": "predicate",
    "LegalizeActions": "action",
}
_IGNORED_NAMESPACES = {"std", "llvm", "MIPatternMatch", "TargetOpcode",
                       "CodeModel", "LegalizeResult"}
_DECL_KEYWORDS = {"return", "if", "while", "for", "switch", "case",
                  "sizeof", "new", "delete", "throw", "co_await",
                  "decltype", "alignof", "static_cast", "reinterpret_cast",
                  "const_cast", "dynamic_cast", "assert"}


def _classify(name, receiver, qual, scope, index, names, table,
              target_class, free_names):
    if name in _CPP_IGNORE or name in ("true", "false"):
        return "ignore", "cpp"
    if name in _SUBTARGETS or name in ("TM", "TLI"):
        return "ignore", "variable"
    if name in _QUERY_FIELDS:
        return "exact", "query_api"
    if name in _TARGET_RESULTS:
        return _TARGET_RESULTS[name], "target_query"
    if qual == "::" and receiver in (target_class, "AArch64", "X86"):
        return "ignore", "target_method"
    if qual == "::" and receiver in _IGNORED_NAMESPACES:
        return "ignore", "cxx_namespace"
    if qual == "::" and receiver in _TYPE_NAMESPACES:
        section = _TYPE_NAMESPACES[receiver]
        if name in names.get(section, ()):
            return "exact", section
        if receiver == "LLT" and name in _dsl.llt_query_names(table):
            return "exact", "llt_query"
        return "unknown", "qualified_namespace"
    if qual == "::":
        return "unknown", "qualifier"
    if qual in (".", "->"):
        if (receiver in _SUBTARGETS or receiver in ("TM", "TLI")) and \
                name.startswith(("has", "is", "use", "get", "can", "outline")):
            return "exact", "subtarget_predicate"
        if name in free_names:
            return "exact", "target_function"
        entry = index.get(name)
        if entry:
            return entry[0], entry[1]
        return "unknown", "member"
    if name in free_names:
        return "exact", "target_function"
    entry = index.get(name)
    if entry:
        return entry[0], entry[1]
    return "unknown", "call"


def audit_text(text, target, table=None, source_path=None, revision=None):
    table = table or _dsl.load()
    index = _entry_index(table)
    toks = lex(text)
    class_name = _class_name(text)
    funcs, definitions = _function_scopes(toks, class_name)
    ctor = next((f for f in funcs if f["kind"] == "constructor"), None)
    lambdas = _lambda_scopes(toks, ctor["start"], ctor["end"]) if ctor else []
    funcs.extend(_free_function_scopes(toks, funcs))
    names = _section_names(table)
    free_names = {f["name"] for f in funcs if f["kind"] == "lowering"}
    free_names.update(entry["name"] for entry in table["target_functions"]
                      if "::" not in entry["name"])
    free_names.add(class_name)

    counts = {scope: Counter() for scope in
              ("constructor", "lambda", "lowering", "file")}
    unknown = []
    opaque = []
    surface = set()
    for i, tok in enumerate(toks):
        if tok.kind != "id" or i in definitions:
            continue
        is_call = i + 1 < len(toks) and toks[i + 1].text == "("
        is_member = i >= 1 and toks[i - 1].text in (".", "->")
        if not (is_call or is_member):
            continue
        prev = toks[i - 1].text if i else None
        if (prev == ">" or (prev and prev not in ("(", ",", "=", "return")
                            and prev not in _DECL_KEYWORDS
                            and toks[i - 1].kind == "id"
                            and i + 1 < len(toks) and toks[i + 1].text == "("
                            and prev not in index and prev not in free_names)):
            # ``SmallVector<LLT, 8> Name(list);`` -- a declaration, not a call.
            continue
        receiver, qual = _receiver(toks, i)
        scope = _scope_at(i, funcs, lambdas)
        resolution, detail = _classify(
            tok.text, receiver, qual, scope, index, names, table, class_name,
            free_names)
        counts[scope][resolution] += 1
        if scope in ("constructor", "lambda"):
            surface.add(f"{scope}|{tok.text}|{qual or ''}|{resolution}")
        if resolution == "unknown":
            unknown.append({"name": tok.text, "line": tok.line,
                            "scope": scope, "receiver": receiver,
                            "detail": detail})
        elif resolution == "opaque":
            opaque.append({"name": tok.text, "line": tok.line,
                           "scope": scope, "receiver": receiver,
                           "detail": detail})
    payload = "\n".join(sorted(surface)).encode("utf-8")
    return {
        "schema": SCHEMA,
        "version": VERSION,
        "target": target,
        "revision": revision,
        "source": source_path,
        "class": class_name,
        "scopes": {
            scope: {key: counter.get(key, 0) for key in
                    ("exact", "opaque", "ignore", "unknown")}
            for scope, counter in counts.items()
        },
        "unknown": unknown,
        "opaque": opaque,
        "surface_sha256": hashlib.sha256(payload).hexdigest(),
        "symbols": len(surface),
    }


def audit_source(target, repo=None, revision=None, source_path=None,
                 text=None, table=None):
    if text is None:
        profile_path = source_path
        if profile_path is None:
            from . import targets
            profile_path = targets.get(target).source_path
        if revision is None:
            revision = _pin.revision(target=target)
        if not repo or not revision:
            raise ValueError("audit needs either text, or repo and revision")
        proc = subprocess.run(
            ["git", "-C", repo, "show", f"{revision}:{profile_path}"],
            capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip()
                               or f"git show {revision}:{profile_path} failed")
        text = proc.stdout
        source_path = profile_path
    return audit_text(text, target, table=table, source_path=source_path,
                      revision=revision)


def manifest_path(target):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "assets", "targets", f"{target}.surface.json")


def load_manifest(target):
    try:
        with open(manifest_path(target), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


MANIFEST_KEYS = ("schema", "version", "target", "revision", "source",
                 "class", "scopes", "surface_sha256", "symbols")


def write_manifest(report):
    """Write the compact gate manifest (no per-occurrence detail)."""
    path = manifest_path(report["target"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    doc = {key: report.get(key) for key in MANIFEST_KEYS}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1, sort_keys=True)
        handle.write("\n")
    return path


def _unknown_sites(report, scope, limit=5):
    sites = [f"{entry['name']} at line {entry['line']}"
             for entry in report.get("unknown", [])
             if entry.get("scope") == scope]
    if len(sites) > limit:
        sites = [*sites[:limit], f"... and {len(sites) - limit} more"]
    return ", ".join(sites)


def gate(report, strict=True):
    """Failures that ``--strict`` treats as errors."""
    problems = []
    scopes = report["scopes"]
    if scopes.get("constructor", {}).get("unknown"):
        problems.append(
            f"constructor has {scopes['constructor']['unknown']} unknown "
            f"call(s): {_unknown_sites(report, 'constructor')}")
    if scopes.get("lambda", {}).get("unknown"):
        problems.append(
            f"lambda has {scopes['lambda']['unknown']} unknown call(s): "
            f"{_unknown_sites(report, 'lambda')}")
    if strict:
        manifest = load_manifest(report["target"])
        if manifest is None:
            problems.append(f"no surface manifest for {report['target']}")
        elif manifest.get("surface_sha256") != report["surface_sha256"]:
            problems.append(
                f"surface changed for {report['target']}: "
                f"{manifest.get('surface_sha256', 'none')[:12]} -> "
                f"{report['surface_sha256'][:12]}")
    return problems
