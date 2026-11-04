"""Cross-check a C++-derived AST against the TableGen-emitted test metadata.

This is the only place that reads a generated artifact.  It never feeds the
corpus generator; it only reports where the two ledgers differ.
"""

import re


ROW_RE = re.compile(
    r'\{"(X86_[^"]+)",\s*"(G_[^"]+)",\s*\{[^}]*\},\s*\{[^}]*\},\s*'
    r'(?:true|false),\s*\d+,\s*(\d+),\s*(\d+)\}')
CLASS_RE = re.compile(r"\{X86TestKind_(\w+),\s*(\d+),\s*(\d+)\}")


def parse_td_inc(text):
    rows = []
    for match in ROW_RE.finditer(text):
        rows.append({
            "name": match.group(1),
            "opcode": match.group(2),
            "num_rules": int(match.group(3)),
            "num_slots": int(match.group(4)),
        })
    classes = []
    block = text.split("X86TestTypeClasses[]", 1)
    if len(block) == 2:
        for match in CLASS_RE.finditer(block[1].split("};", 1)[0]):
            kind, a, b = match.group(1), int(match.group(2)), int(match.group(3))
            if kind == "Pointer":
                classes.append(f"p{b - 1}")  # stored as Pointer, 0, 1
            elif kind == "Scalar":
                classes.append(f"s{a}")
            elif kind == "Vector":
                classes.append(f"v{b}s{a}")
            elif kind == "PtrVector":
                classes.append(f"v{b}p0")
    return {"rows": rows, "classes": classes}


def build_report(ast, td):
    rows = td["rows"]
    builder_for = {}
    for b in ast["builders"]:
        for opcode in b["opcodes"]:
            builder_for[opcode] = b

    ledger = []
    opcodes_in_td = set()
    for row in rows:
        opcodes_in_td.add(row["opcode"])
        b = builder_for.get(row["opcode"])
        if b is None:
            ledger.append({
                "rule_set": row["name"], "opcode": row["opcode"],
                "chain_rules": None, "td_num_rules": row["num_rules"],
                "delta": None, "class": "no_cpp_builder",
            })
            continue
        chain = len(b["rules"])
        delta = row["num_rules"] - chain
        if delta == 0:
            cls = "match"
        elif delta > 0 and any(r["method"] in ("legalIf", "customIf", "lowerIf")
                              for r in b["rules"]):
            cls = "td_expands_lambda"
        elif delta > 0:
            cls = "td_only"
        else:
            cls = "input_missing"
        ledger.append({
            "rule_set": row["name"], "opcode": row["opcode"],
            "chain_rules": chain, "td_num_rules": row["num_rules"],
            "delta": delta, "class": cls, "cpp_builder": b["name"],
        })

    cpp_opcodes = set(builder_for)
    ast_classes = set(ast["classes"])
    td_classes = set(td["classes"])
    return {
        "census": ast["census"],
        "rule_ledger": ledger,
        "rule_ledger_totals": {
            "chain_rules": sum(x["chain_rules"] or 0 for x in ledger),
            "td_num_rules": sum(x["td_num_rules"] for x in ledger),
        },
        "opcodes_vs_td": {
            "cpp": len(cpp_opcodes),
            "td_rows": len(opcodes_in_td),
            "cpp_not_in_td": sorted(cpp_opcodes - opcodes_in_td),
            "td_not_in_cpp": sorted(opcodes_in_td - cpp_opcodes),
        },
        "classes_vs_td": {
            "cpp": sorted(ast_classes),
            "td": sorted(td_classes),
            "cpp_not_in_td": sorted(ast_classes - td_classes),
            "td_not_in_cpp": sorted(td_classes - ast_classes),
        },
    }


def only_allowed_deltas(report, known):
    """Return the deltas that are neither explained nor registered.

    `known` is a set of (rule_set, class) pairs from known_deltas.json.
    Every non-matching delta must be registered there; the registry is the
    review record for why the C++ chain and the TableGen emission differ.
    """
    bad = []
    for item in report["rule_ledger"]:
        if item["class"] == "match":
            continue
        if (item["rule_set"], item["class"]) in known:
            continue
        bad.append(item)
    return bad
