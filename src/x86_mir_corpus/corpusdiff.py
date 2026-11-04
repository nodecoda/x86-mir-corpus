"""Canonical comparison of two X86 legalizer corpus documents.

The comparison key is the query itself, never an array position, an atom id,
or the raw bytes of the file:

    (config_fingerprint, opcode, types, mem type, align bits)

`expected` is compared only where both sides carry one.  A shapes-only corpus
therefore reports `expected_available: false` instead of pretending that the
answers matched.
"""

import hashlib
import json


def load(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _doc_config(doc):
    desc = doc.get("config_descriptor")
    if desc is not None:
        return desc
    atoms = doc.get("atoms") or doc.get("points") or []
    configs = sorted({a.get("config") for a in atoms if a.get("config")})
    if len(configs) == 1:
        return {"id": configs[0]}
    if configs:
        return {"id": None, "configs": configs}
    return {"id": doc.get("config")}


def config_fingerprint(doc):
    desc = _doc_config(doc)
    text = json.dumps(desc, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def config_id(doc):
    return _doc_config(doc).get("id")


def configs_compatible(left, right):
    """v1 documents have no feature map; an id match is all they can prove."""
    ldesc, rdesc = _doc_config(left), _doc_config(right)
    lid, rid = ldesc.get("id"), rdesc.get("id")
    if lid is None or rid is None:
        # A mixed-config v1 witness has no single id; compare the set.
        return (ldesc.get("configs") is not None
                and ldesc.get("configs") == rdesc.get("configs"))
    if lid != rid:
        return False
    lf, rf = ldesc.get("features"), rdesc.get("features")
    return lf is None or rf is None or lf == rf


def _query(atom):
    query = atom.get("query")
    if query is None:
        query = {
            "types": atom.get("types", []),
            "mem": (None if atom.get("mem") is None
                    else {"type": atom["mem"], "align": atom.get("align", 0)}),
        }
    return query


def _norm_type(name):
    """Pointers are spelled with or without their width across artifacts."""
    if name and (name.startswith("p") or "p" in name.split("s", 1)[0]):
        return name.split(":", 1)[0]
    return name


def atom_key(atom, config, opcode=None):
    query = _query(atom)
    mem = query.get("mem") or {}
    return (
        atom.get("config") or config,
        opcode or atom.get("opcode"),
        tuple(_norm_type(t) for t in (query.get("types") or ())),
        _norm_type(mem.get("type")),
        mem.get("align", 0),
    )


def key_order(key):
    """Total order for keys whose opcode or memory type may be absent."""
    config, opcode, types, mem_type, align = key
    return (config or "", opcode or "", types, mem_type or "", align)


def render_key(key):
    config, opcode, types, mem_type, align = key
    out = {"config": config, "opcode": opcode, "types": list(types)}
    if mem_type is not None:
        out["mem"] = {"type": mem_type, "align": align}
    return out


def _atoms(doc):
    return doc.get("atoms") or doc.get("points") or []


def opcode_map(*docs):
    """Unique rule_set -> opcode mappings supplied by either side.

    v1 witness atoms have no opcode field.  A rule set with exactly one opcode
    on the v2 side can be mapped unambiguously; a multi-opcode builder is left
    unresolved and reported.
    """
    mapping = {}
    for doc in docs:
        for atom in _atoms(doc):
            rule_set, opcode = atom.get("rule_set"), atom.get("opcode")
            if rule_set and opcode:
                mapping.setdefault(rule_set, set()).add(opcode)
    return {name: next(iter(ops)) for name, ops in mapping.items()
            if len(ops) == 1}


def unmapped_rule_sets(*docs):
    mapping = opcode_map(*docs)
    missing = set()
    for doc in docs:
        for atom in _atoms(doc):
            if not atom.get("opcode") and not mapping.get(atom.get("rule_set")):
                missing.add(atom.get("rule_set"))
    return sorted(name for name in missing if name)


def _index(doc, opcodes):
    config = config_id(doc)
    index = {}
    for atom in _atoms(doc):
        key = atom_key(atom, config, opcodes.get(atom.get("rule_set")))
        index.setdefault(key, []).append(atom)
    return index


def _expected(atom):
    value = atom.get("expected")
    return None if value is None else json.dumps(value, sort_keys=True)


def _compare_expected(left, right):
    left_values = sorted(v for v in (_expected(a) for a in left) if v is not None)
    right_values = sorted(v for v in (_expected(a) for a in right) if v is not None)
    if not left_values or not right_values:
        return None
    return left_values == right_values, left_values, right_values


def _per_rule(index):
    counts = {}
    for _key, atoms in index.items():
        for atom in atoms:
            name = atom.get("rule_set")
            counts[name] = counts.get(name, 0) + 1
    return counts


def compare(left, right, left_name="left", right_name="right"):
    left_fp, right_fp = config_fingerprint(left), config_fingerprint(right)
    opcodes = opcode_map(left, right)
    left_index = _index(left, opcodes)
    right_index = _index(right, opcodes)

    left_only = sorted(set(left_index) - set(right_index), key=key_order)
    right_only = sorted(set(right_index) - set(left_index), key=key_order)
    common = sorted(set(left_index) & set(right_index), key=key_order)

    expected_diff = []
    expected_compared = 0
    for key in common:
        got = _compare_expected(left_index[key], right_index[key])
        if got is None:
            continue
        expected_compared += 1
        if not got[0]:
            expected_diff.append({
                "query": render_key(key),
                left_name: got[1],
                right_name: got[2],
            })

    left_rules, right_rules = _per_rule(left_index), _per_rule(right_index)
    per_rule = {
        "left": dict(sorted(left_rules.items())),
        "right": dict(sorted(right_rules.items())),
        "left_only": sorted(set(left_rules) - set(right_rules)),
        "right_only": sorted(set(right_rules) - set(left_rules)),
        "count_diff": [
            {"rule_set": name, "left": left_rules.get(name, 0),
             "right": right_rules.get(name, 0)}
            for name in sorted(set(left_rules) | set(right_rules))
            if left_rules.get(name, 0) != right_rules.get(name, 0)
        ],
    }

    return {
        "schema": "llvm-x86-legalizer-corpus-diff",
        "version": 2,
        "left": {"path": None, "config": config_id(left),
                 "config_fingerprint": left_fp,
                 "atoms": sum(len(v) for v in left_index.values())},
        "right": {"path": None, "config": config_id(right),
                  "config_fingerprint": right_fp,
                  "atoms": sum(len(v) for v in right_index.values())},
        "config_match": configs_compatible(left, right),
        "unmapped_rule_sets": unmapped_rule_sets(left, right),
        "keys": {"left": len(left_index), "right": len(right_index),
                 "common": len(common),
                 "left_only": len(left_only), "right_only": len(right_only)},
        "expected_available": expected_compared > 0,
        "expected_compared": expected_compared,
        "expected_diff": expected_diff,
        "left_only": [render_key(k) for k in left_only],
        "right_only": [render_key(k) for k in right_only],
        "per_rule_coverage": per_rule,
    }


def migrate_v1(doc, rule_opcodes, source=None):
    """Rewrite a v1 document into the v2 atom shape without touching it.

    v1 witness atoms predate the `opcode` field; they were emitted for a
    builder's primary opcode.  The caller supplies that mapping from the
    source revision recorded in the document.  Returns None when the input is
    already v2.
    """
    if doc.get("version") != 1:
        return None
    atoms = []
    missing = set()
    for atom in _atoms(doc):
        out = dict(atom)
        if "query" not in out:
            out["query"] = _query(out)
        if not out.get("opcode"):
            opcode = rule_opcodes.get(out.get("rule_set"))
            if opcode is None:
                missing.add(out.get("rule_set"))
            else:
                out["opcode"] = opcode
        atoms.append(out)
    if missing:
        raise ValueError("cannot infer opcode for rule sets: "
                         + ", ".join(sorted(name for name in missing if name)))
    result = dict(doc)
    result["schema"] = "llvm-x86-legalizer-cpp-corpus"
    result["version"] = 2
    result["migrated_from_version"] = 1
    result.pop("points", None)
    result["atoms"] = atoms
    result["atom_count"] = len(atoms)
    if source is not None:
        result["migration_source"] = source
    configs = sorted({a.get("config") for a in atoms if a.get("config")})
    if len(configs) == 1:
        result["config"] = configs[0]
    return result


def has_differences(report):
    return bool(report["expected_diff"] or report["left_only"]
                or report["right_only"] or not report["config_match"])
