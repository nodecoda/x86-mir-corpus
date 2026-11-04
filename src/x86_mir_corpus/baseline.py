"""Stable projections of MIR reports for baseline capture and checks.

A baseline is a known set, not a claim that every atom is healthy.  The check
reports only what changed relative to that set (added/removed atoms), so an
A3 failure stays visible without turning into a spurious baseline failure.
"""

import json
import os

from . import schemas

PROJECTION_FIELDS = ("outcome", "steps", "detail_code", "failed_on_opcode")


def _atom_projection(atom):
    projected = {"id": atom["id"]}
    for field in PROJECTION_FIELDS:
        projected[field] = atom.get(field)
    return projected


def project(report):
    """The deterministic projection of a mir-run report."""
    atoms = sorted((_atom_projection(atom) for atom in report["atoms"]),
                   key=lambda atom: atom["id"])
    corpus = report.get("corpus") or {}
    return {
        "schema": schemas.BASELINE_PROJECTION_SCHEMA,
        "version": schemas.BASELINE_PROJECTION_VERSION,
        "config": report.get("config") or corpus.get("config"),
        "corpus": {
            "atoms": corpus.get("atoms"),
            "sha256": corpus.get("sha256"),
        },
        "counts": report.get("counts"),
        "atom_count": len(atoms),
        "atoms": atoms,
    }


def load(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write(path, doc):
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1, sort_keys=True)
        handle.write("\n")


def capture(report_path, out_path):
    doc = project(load(report_path))
    write(out_path, doc)
    return doc


def check(report_path, baseline_path):
    """Compare a report against a baseline projection."""
    actual = project(load(report_path))
    baseline = load(baseline_path)
    if baseline.get("schema") != schemas.BASELINE_PROJECTION_SCHEMA:
        raise SystemExit(
            f"{baseline_path} is not a {schemas.BASELINE_PROJECTION_SCHEMA} "
            f"document (got {baseline.get('schema')!r}); capture it with "
            "'baseline capture'")
    left = {atom["id"]: atom for atom in baseline.get("atoms", [])}
    right = {atom["id"]: atom for atom in actual["atoms"]}
    added = [right[key] for key in sorted(set(right) - set(left))]
    removed = [left[key] for key in sorted(set(left) - set(right))]
    changed = []
    for key in sorted(set(left) & set(right)):
        if left[key] != right[key]:
            removed.append(left[key])
            added.append(right[key])
            changed.append({
                "id": key,
                "before": {f: left[key].get(f) for f in PROJECTION_FIELDS},
                "after": {f: right[key].get(f) for f in PROJECTION_FIELDS},
            })
    added.sort(key=lambda atom: atom["id"])
    removed.sort(key=lambda atom: atom["id"])
    return {
        "schema": "llvm-x86-legalizer-mir-baseline-check",
        "version": 1,
        "ok": not added and not removed,
        "baseline": os.path.abspath(baseline_path),
        "report": os.path.abspath(report_path),
        "atom_count": actual["atom_count"],
        "baseline_atom_count": len(left),
        "added": added,
        "removed": removed,
        "changed": changed,
        "counts_changed": baseline.get("counts") != actual.get("counts"),
    }
