"""Deterministic JSON writers and the AST/points document shapes."""

import json
import os

from . import expr as E

AST_SCHEMA = "llvm-x86-legalizer-cpp-ast"
POINTS_SCHEMA = "llvm-x86-legalizer-points"
VERSION = 2


def write_json(path, obj):
    data = json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(data)


def render_value(value):
    """A JSON-friendly rendering of a parse-time evaluated argument."""
    if E.is_llt(value):
        return E.llt_name(value)
    if isinstance(value, list):
        return [render_value(v) for v in value]
    if isinstance(value, (bool, int, str)):
        return value
    return None


def ast_document(result, source):
    builders = []
    for b in result.builders:
        rules = []
        for r in b.rules:
            rules.append({
                "index": r.index,
                "method": r.method,
                "line": r.line,
                "guard": [{"text": text, "value": value} for text, value in r.guard],
                "active": r.active,
                "arg": r.arg_text[0] if r.arg_text else "",
                "arg_values": [render_value(v) for v in r.evaluated_args],
                "slots": sorted(b.slots),
            })
        builders.append({
            "name": b.name,
            "opcodes": list(b.opcodes),
            "num_slots": b.num_slots,
            "has_mem": b.has_mem,
            "line": b.line,
            "aligns": sorted(b.aligns),
            "mem_classes": [E.llt_name(t) for t in sorted(b.mem_classes, key=repr)],
            "rules": rules,
        })
    return {
        "schema": AST_SCHEMA,
        "version": VERSION,
        "source": source,
        "config": result.config,
        "features": result.features,
        "census": result.census(),
        "classes": [E.llt_name(t) for t in sorted(result.classes, key=repr)],
        "mem_classes": [E.llt_name(t) for t in sorted(result.mem_classes, key=repr)],
        "aligns": sorted(result.aligns),
        "unknown_methods": [{"method": m, "line": line}
                            for m, line in result.unknown_methods],
        "unknown_predicates": [{"predicate": p, "line": line}
                               for p, line in result.unknown_predicates],
        "unknown_refs": [{"name": n, "line": line, "error": err}
                         for n, line, err in result.unknown_refs],
        "opaque_lambdas": result.opaque_lambdas,
        "builders": builders,
    }


def points_document(result, source, tier, points, classes, mem_classes, panel,
                    pruned=0, coverage=None, oracle=None,
                    config_descriptor=None):
    counts = {}
    for p in points:
        counts[p["origin"]] = counts.get(p["origin"], 0) + 1
    doc = {
        "schema": POINTS_SCHEMA,
        "version": VERSION,
        "source": source,
        "config": result.config,
        "features": result.features,
        "tier": tier,
        "classes": [E.llt_name(t) for t in classes],
        "mem_classes": [E.llt_name(t) for t in mem_classes],
        "panel": [E.llt_name(t) for t in panel],
        "origin_counts": counts,
        "point_count": len(points),
        "pruned": {"pointer_element_size": pruned} if pruned else {},
        "points": points,
    }
    if oracle is not None:
        doc["oracle"] = oracle
    if config_descriptor is not None:
        doc["config_descriptor"] = config_descriptor
    if coverage is not None:
        doc["coverage"] = coverage
    doc["counts"] = {
        "points": len(points),
        "origins": counts,
        "pruned_pointer_element_size": pruned,
    }
    if coverage is not None:
        doc["counts"].update({
            "rules_source_total": coverage["rules"]["source_total"],
            "rules_active": coverage["rules"]["active"],
            "rules_verified": coverage["rules"]["verified"],
            "rules_excepted": len(coverage["rules"]["excepted"]),
            "rules_unprobed": len(coverage["rules"]["unprobed"]),
            "opcodes_total": coverage["opcodes"]["total"],
            "opcodes_uncovered": len(coverage["opcodes"]["uncovered"]),
        })
    return doc
