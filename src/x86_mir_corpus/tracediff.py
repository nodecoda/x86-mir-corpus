"""Canonical comparison of two key -> result legalizer traces.

Both trace drivers -- the frozen C++ reference and the generated table -- write
the same document shape: one record per corpus atom, carrying the terminal
`status`, the final state, and the ordered list of first steps taken to reach
it.  This module compares records by atom id and by behaviour, never by array
position:

    id  ->  (rounds, status, final_types, final_mem, [(action, idx, to), ...])

`answer_kind` is provenance, not behaviour: the table records whether a step
came from a cell, from the evaluator asked directly, or from an escape, while
the reference has no such field.  It is summarised separately and never counts
as a difference.
"""

import json


SCHEMA = "llvm-x86-legalizer-cpp-trace"


def load(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _atoms(doc):
    return doc.get("atoms") or []


def atom_id(atom):
    return atom.get("id")


def trace_steps(atom):
    """The behavioural fields of one step, in a stable shape.

    `answer_kind` is dropped on purpose: it says how the oracle answered, not
    what it answered.
    """
    steps = []
    for step in atom.get("trace") or []:
        out = {"action": step.get("action")}
        if "idx" in step:
            out["idx"] = step["idx"]
        if "to" in step:
            out["to"] = step["to"]
        steps.append(out)
    return steps


def behaviour(atom):
    return {
        "rounds": atom.get("rounds"),
        "status": atom.get("status"),
        "final_types": atom.get("final_types"),
        "final_mem": atom.get("final_mem"),
        "trace": trace_steps(atom),
    }


def _histogram(values):
    counts = {}
    for value in values:
        key = "null" if value is None else str(value)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def answer_kinds(doc):
    counts = {}
    for atom in _atoms(doc):
        for step in atom.get("trace") or []:
            kind = step.get("answer_kind")
            if kind is not None:
                counts[kind] = counts.get(kind, 0) + 1
    return dict(sorted(counts.items()))


def _index(doc):
    index = {}
    duplicates = []
    for atom in _atoms(doc):
        key = atom_id(atom)
        if key is None:
            raise ValueError("trace atom has no id")
        if key in index:
            duplicates.append(key)
        index[key] = atom
    return index, duplicates


def _first_divergence(left, right):
    """Index of the first step that differs, or None when one is a prefix."""
    for index in range(min(len(left), len(right))):
        if left[index] != right[index]:
            return index
    if len(left) != len(right):
        return min(len(left), len(right))
    return None


def _diff(key, left, right):
    lb, rb = behaviour(left), behaviour(right)
    index = _first_divergence(lb["trace"], rb["trace"])
    entry = {
        "id": key,
        "config": left.get("config") or right.get("config"),
        "opcode": left.get("opcode") or right.get("opcode"),
        "rule_set": left.get("rule_set") or right.get("rule_set"),
        "start_types": left.get("start_types"),
        "start_mem": left.get("start_mem"),
        "left": {
            "status": lb["status"],
            "rounds": lb["rounds"],
            "final_types": lb["final_types"],
            "final_mem": lb["final_mem"],
        },
        "right": {
            "status": rb["status"],
            "rounds": rb["rounds"],
            "final_types": rb["final_types"],
            "final_mem": rb["final_mem"],
        },
        "first_divergence": index,
    }
    if index is not None:
        entry["divergence"] = {
            "index": index,
            "left_step": (lb["trace"][index]
                          if index < len(lb["trace"]) else None),
            "right_step": (rb["trace"][index]
                           if index < len(rb["trace"]) else None),
        }
    return entry


def compare(left, right, left_name="left", right_name="right", max_diffs=50):
    left_index, left_dup = _index(left)
    right_index, right_dup = _index(right)

    left_only = sorted(set(left_index) - set(right_index))
    right_only = sorted(set(right_index) - set(left_index))
    common = sorted(set(left_index) & set(right_index))

    diffs = []
    classes = {}
    for key in common:
        lb = behaviour(left_index[key])
        rb = behaviour(right_index[key])
        if lb == rb:
            continue
        if len(diffs) < max_diffs:
            diffs.append(_diff(key, left_index[key], right_index[key]))
        name = f"{lb['status']} -> {rb['status']}"
        classes[name] = classes.get(name, 0) + 1

    left_longest = max((behaviour(a).get("rounds") or 0)
                       for a in left_index.values()) if left_index else 0
    right_longest = max((behaviour(a).get("rounds") or 0)
                        for a in right_index.values()) if right_index else 0

    left_sha = left.get("corpus_sha256")
    right_sha = right.get("corpus_sha256")
    corpus_match = None
    if left_sha is not None and right_sha is not None:
        corpus_match = left_sha == right_sha
    left_config, right_config = left.get("config"), right.get("config")
    config_match = None
    if left_config is not None and right_config is not None:
        config_match = left_config == right_config

    return {
        "schema": "llvm-x86-legalizer-trace-diff",
        "version": 1,
        "left": {
            "path": None,
            "oracle": left.get("oracle"),
            "config": left_config,
            "atoms": len(left_index),
            "corpus_sha256": left_sha,
        },
        "right": {
            "path": None,
            "oracle": right.get("oracle"),
            "config": right_config,
            "atoms": len(right_index),
            "corpus_sha256": right_sha,
        },
        "corpus_match": corpus_match,
        "config_match": config_match,
        "duplicate_ids": {"left": len(left_dup), "right": len(right_dup)},
        "ids": {
            "left": len(left_index),
            "right": len(right_index),
            "common": len(common),
            "left_only": len(left_only),
            "right_only": len(right_only),
            "left_only_sample": left_only[:max_diffs],
            "right_only_sample": right_only[:max_diffs],
        },
        "status_histogram": {
            left_name: _histogram(
                a.get("status") for a in left_index.values()),
            right_name: _histogram(
                a.get("status") for a in right_index.values()),
        },
        "rounds_histogram": {
            left_name: _histogram(
                a.get("rounds") for a in left_index.values()),
            right_name: _histogram(
                a.get("rounds") for a in right_index.values()),
        },
        "longest_rounds": {left_name: left_longest, right_name: right_longest},
        "answer_kinds": {
            left_name: answer_kinds(left),
            right_name: answer_kinds(right),
        },
        "diff_count": sum(classes.values()),
        "classes": [
            {"transition": name, "count": count}
            for name, count in sorted(classes.items(),
                                      key=lambda item: (-item[1], item[0]))
        ],
        "diffs": diffs,
    }


def has_differences(report):
    ids = report["ids"]
    return bool(report["diff_count"] or ids["left_only"]
                or ids["right_only"])
