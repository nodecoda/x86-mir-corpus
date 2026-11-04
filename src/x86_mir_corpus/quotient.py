"""Finite-quotient analysis over a target legalizer.

The key of a query is the bit vector of every modelled legality-predicate
result that can read it.  Two queries are equivalent iff every predicate in the
same dependency component answers identically; ``None`` (an unmodelled
predicate) is kept as a third value instead of being guessed.

Axes are dependency components rather than hand-picked panels:

* ``slot<i>`` / ``pair(i,j)`` / ``triple(i,j,k)`` from predicate slot reads;
* ``mem`` and ``align`` when a predicate reads them;
* ``config`` and ``opcode`` as discrete vocabulary axes.

The candidate universe is the parsed class list (not a sample), so the
reported class count is exact for the rule-set predicates the parser models.
Lambdas that the expression layer cannot reduce are counted as opaque and the
affected rule sets are flagged, never silently treated as false.
"""

import itertools

from . import domain
from . import dsl as _dsl
from . import expr as E

_TABLE = _dsl.load()
_PREDICATES = {entry["name"]: entry for entry in _TABLE["predicates"]}


def _roles(name, nargs):
    entry = _PREDICATES.get(name)
    if entry is None:
        return []
    overloads = entry.get("overloads") or []
    exact = [o for o in overloads if o["arity"] == nargs]
    candidates = exact or [o for o in overloads
                           if len(o["roles"]) == nargs]
    if not candidates:
        return list(entry.get("roles") or ())
    return list(candidates[0]["roles"])


def _arg_slot(arg, env):
    try:
        value = E.eval_expr(arg, env)
    except E.ExprError:
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _walk_predicates(node, env, out):
    """Collect predicate calls depth-first, including nested ``all``/``any``."""
    if isinstance(node, E.Call):
        name = node.name.rsplit("::", 1)[-1]
        if name in domain.PREDICATE_IMPL:
            roles = _roles(name, len(node.args))
            slots = tuple(sorted({
                idx for role, arg in zip(roles, node.args, strict=False)
                if role == "slot"
                for idx in (_arg_slot(arg, env),) if idx is not None}))
            out.append({
                "node": node,
                "name": name,
                "slots": slots,
                "mem": "mem" in roles,
                "align": "mem_desc_list" in roles,
            })
        for arg in node.args:
            _walk_predicates(arg, env, out)
    elif isinstance(node, E.Binary):
        _walk_predicates(node.left, env, out)
        _walk_predicates(node.right, env, out)
    elif isinstance(node, E.Unary):
        _walk_predicates(node.x, env, out)
    elif isinstance(node, E.Ternary):
        _walk_predicates(node.cond, env, out)
        _walk_predicates(node.then, env, out)
        _walk_predicates(node.other, env, out)
    elif isinstance(node, E.ListLit):
        for item in node.items:
            _walk_predicates(item, env, out)


def builder_predicates(builder):
    """Every modelled predicate mentioned by *builder*'s rules.

    Returns ``(predicates, opaque_lambdas)``.  Predicate calls inside lambda
    bodies are not recovered here; the count is reported instead so a rule set
    is never presented as fully modelled when it is not.
    """
    env = dict(getattr(builder, "locals", None) or {})
    predicates = []
    opaque = 0
    for rule in builder.rules:
        for arg in rule.args:
            if isinstance(arg, E.Lambda):
                opaque += 1
                continue
            _walk_predicates(arg, env, predicates)
    unique = {}
    for pred in predicates:
        key = (pred["name"], pred["slots"], pred["mem"], pred["align"],
               repr(pred["node"]))
        unique.setdefault(key, pred)
    return list(unique.values()), opaque


class _UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, item):
        self.parent.setdefault(item, item)
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, items):
        roots = [self.find(item) for item in items]
        for root in roots[1:]:
            if root != roots[0]:
                self.parent[root] = roots[0]

    def groups(self):
        groups = {}
        for item in self.parent:
            groups.setdefault(self.find(item), set()).add(item)
        return [tuple(sorted(group, key=str))
                for group in sorted(groups.values(), key=lambda g: sorted(g, key=str))]


def _rule_read_set(rule, builder):
    """Slots and mem/align a rule's match condition reads.

    List rules (``legalFor*``) couple every column of an entry; cartesian
    rules read one slot per list.  Predicate-gated rules read their predicate
    slots.  A rule whose lambda cannot be reduced is conservatively charged
    with every slot, which keeps the quotient sound (coarser, never finer).
    """
    slots = set()
    mem = False
    align = False
    method = rule.method
    args = list(getattr(rule, "evaluated_args", None) or ())
    if method in ("legalFor", "customFor", "lowerFor", "libcallFor"):
        for arg in args:
            if not isinstance(arg, list):
                continue
            for entry in arg:
                if isinstance(entry, list):
                    slots.update(range(len(entry)))
    elif method == "legalForTypesWithMemDesc":
        mem = True
        for arg in args:
            if not isinstance(arg, list):
                continue
            for entry in arg:
                if isinstance(entry, list) and len(entry) >= 2:
                    slots.update(range(len(entry) - 2))
    elif "CartesianProduct" in method:
        lists = [value for value in args if isinstance(value, list)]
        slots.update(range(len(lists)))
    predicates, opaque = builder_predicates(_single_rule_builder(builder, rule))
    for pred in predicates:
        slots.update(pred["slots"])
        mem = mem or pred["mem"]
        align = align or pred["align"]
    if opaque:
        slots.update(range(builder.num_slots))
    return tuple(sorted(slots)), mem, align


def _single_rule_builder(builder, rule):
    return type("_RuleView", (), {
        "name": builder.name,
        "opcodes": builder.opcodes,
        "rules": [rule],
        "locals": getattr(builder, "locals", None) or {},
        "num_slots": builder.num_slots,
        "slots": builder.slots,
        "has_mem": builder.has_mem,
        "mem_classes": builder.mem_classes,
        "aligns": builder.aligns,
    })()


def read_components(builder):
    """Distinct rule read sets for *builder*, including mem/align axes."""
    components = {}
    for rule in builder.rules:
        if rule.active is False:
            continue
        slots, mem, align = _rule_read_set(rule, builder)
        if not slots and not mem and not align:
            continue
        component = tuple(list(slots)
                          + (["mem"] if mem else [])
                          + (["align"] if align else []))
        components.setdefault(component, 0)
        components[component] += 1
    return sorted(components, key=lambda item: (len(item), [str(x) for x in item]))


def dependency_components(predicates):
    """Group slots that occur together in one predicate into components."""
    union = _UnionFind()
    for pred in predicates:
        if pred["slots"]:
            union.union(pred["slots"])
        elif pred["mem"]:
            union.union(("mem",))
        elif pred["align"]:
            union.union(("align",))
    return union.groups()


def axis_name(component):
    slots = [item for item in component if isinstance(item, int)]
    extra = [item for item in component if not isinstance(item, int)]
    parts = []
    if slots:
        if len(slots) == 1:
            parts.append(f"slot{slots[0]}")
        elif len(slots) == 2:
            parts.append(f"pair({slots[0]},{slots[1]})")
        elif len(slots) == 3:
            parts.append(f"triple({slots[0]},{slots[1]},{slots[2]})")
        else:
            parts.append("tuple(" + ",".join(str(s) for s in slots) + ")")
    parts.extend(extra)
    return "+".join(parts) or "config"


def _axis_domain(component, classes, mem_classes, aligns):
    domain_values = []
    for item in component:
        if item == "mem":
            domain_values.append(list(mem_classes))
        elif item == "align":
            domain_values.append(list(aligns))
        else:
            domain_values.append(list(classes))
    return domain_values


def _query(component, assignment, classes, mem_classes, aligns, num_slots):
    rep = classes[0]
    mem = (mem_classes or [None])[0]
    align = (aligns or [0])[0]
    types = [rep] * max(1, num_slots)
    for item, value in zip(component, assignment, strict=False):
        if item == "mem":
            mem = value
        elif item == "align":
            align = value
        else:
            types[item] = value
    return types, mem, align


def _signature(builder, types, mem, align, env):
    values = []
    for opcode in builder.opcodes:
        verdict, rule_index = domain.first_match(
            builder, types, mem, align, env, opcode)
        if verdict == "unknown":
            values.append(("unknown", rule_index))
        elif verdict == "match":
            values.append(("match", rule_index))
        else:
            values.append(("none", None))
    return tuple(values)


def _panel_query(component, value, classes, num_slots):
    rep = classes[0]
    slots = [item for item in component if isinstance(item, int)]
    if not slots:
        return None
    types = [rep] * max(1, num_slots)
    types[slots[0]] = value
    return types


def _signature_label(signature):
    """JSON-friendly identity of a quotient key (per opcode first match)."""
    return [f"{verdict}@{rule}" for verdict, rule in signature]


def _unrepresented_entry(builder, component, signature, assignment):
    """One uncovered class, with a representative and a reason.

    ``opaque_predicate`` means the rule set reads a predicate the expression
    layer cannot reduce, so the class boundary is not trustworthy yet;
    ``panel_gap`` means a modelled class the boundary panel simply misses and
    the next panel revision should cover.
    """
    reason = ("opaque_predicate"
              if any(verdict == "unknown" for verdict, _ in signature)
              else "panel_gap")
    return {
        "builder": builder.name,
        "signature": _signature_label(signature),
        "example": axis_repr(component, assignment),
        "reason": reason,
    }


def analyze_builder(builder, profile, config, classes, mem_classes, aligns):
    _, opaque = builder_predicates(builder)
    env = domain.probe_env(classes, profile.feature_env(
        profile.configs[config]), profile.llt_extended)
    env.update(getattr(builder, "locals", None) or {})
    axes = {}
    for component in read_components(builder):
        name = axis_name(component)
        values = _axis_domain(component, classes, mem_classes, aligns)
        if any(not group for group in values):
            axes[name] = {"keys": 0, "length": 0, "representatives": [],
                          "unrepresented": 0, "unrepresented_detail": [],
                          "oversampled": 0,
                          "opaque": True, "candidates": 0}
            continue
        seen = {}
        order = []
        total = 0
        for assignment in itertools.product(*values):
            total += 1
            types, mem, align = _query(component, assignment, classes,
                                       mem_classes, aligns, builder.num_slots)
            signature = _signature(builder, types, mem, align, env)
            if signature not in seen:
                seen[signature] = assignment
                order.append(signature)
        reps = [seen[signature] for signature in order]
        panel_hits = {}
        for value in profile.boundary_panel:
            types = _panel_query(component, value, classes, builder.num_slots)
            if types is None:
                continue
            signature = _signature(builder, types,
                                   (mem_classes or [None])[0],
                                   (aligns or [0])[0], env)
            panel_hits[signature] = panel_hits.get(signature, 0) + 1
        unrepresented = [
            signature for signature in order if signature not in panel_hits]
        oversampled = [
            signature for signature, count in panel_hits.items() if count > 1]
        axes[name] = {
            "keys": len(order),
            "length": total,
            "representatives": [axis_repr(component, assignment)
                                for assignment in reps],
            "unrepresented": len(unrepresented),
            "unrepresented_detail": [
                _unrepresented_entry(builder, component, signature,
                                     seen[signature])
                for signature in unrepresented],
            "oversampled": len(oversampled),
            "opaque": any("unknown" in signature for signature in order),
            "candidates": total,
        }
    axes["config"] = {
        "keys": 1, "length": 1, "representatives": [config],
        "unrepresented": 0, "unrepresented_detail": [], "oversampled": 0,
        "opaque": bool(opaque),
        "candidates": 1,
        "note": "run once per config; the corpus config axis is the profile",
    }
    axes["opcode"] = {
        "keys": len(builder.opcodes),
        "length": len(builder.opcodes),
        "representatives": list(builder.opcodes),
        "unrepresented": 0, "unrepresented_detail": [], "oversampled": 0,
        "opaque": bool(opaque),
        "candidates": len(builder.opcodes),
    }
    return axes, opaque


def axis_repr(component, assignment):
    return [value if not E.is_llt(value) else E.llt_name(value)
            for value in assignment]


def _rule_coverage(builder, classes, mem_classes, aligns, env):
    """Which active rule indices are the first match of some class tuple."""
    slots = sorted(builder.slots)
    if not slots:
        slots = list(range(builder.num_slots))
    value_sets = [classes for _ in slots]
    mem = (mem_classes or [None])[0]
    align = (aligns or [0])[0]
    matched = set()
    for assignment in itertools.product(*value_sets):
        types = [classes[0]] * builder.num_slots
        for slot, value in zip(slots, assignment, strict=False):
            types[slot] = value
        for opcode in builder.opcodes:
            verdict = domain.first_match(builder, types, mem, align, env,
                                         opcode)
            if verdict[0] == "match":
                matched.add(verdict[1])
    active = {rule.index for rule in builder.rules if rule.active is not False}
    return len(active & matched), len(active - matched)


def analyze(result, profile, config):
    """Return the finite-quotient report for one parsed legalizer."""
    classes = domain.sorted_llts(result.classes)
    mem_classes = domain.sorted_llts(result.mem_classes)
    aligns = sorted(result.aligns)
    axes = {}
    per_builder = {}
    opaque_builders = []
    covered = uncovered = 0
    env = domain.probe_env(classes, profile.feature_env(
        profile.configs[config]), profile.llt_extended)
    for position, builder in enumerate(result.builders):
        builder_axes, opaque = analyze_builder(
            builder, profile, config, classes, mem_classes, aligns)
        if opaque:
            opaque_builders.append(builder.name)
        key = builder.name
        if key in per_builder:
            key = f"{builder.name}#{position}"
        per_builder[key] = builder_axes
        hits, misses = _rule_coverage(builder, classes, mem_classes, aligns,
                                      dict(env, **(builder.locals or {})))
        covered += hits
        uncovered += misses
    present = {}
    for builder_axes in per_builder.values():
        for name, axis in builder_axes.items():
            if name in ("config", "opcode"):
                continue
            present.setdefault(name, []).append(axis)
    for name, entries in present.items():
        merged = {}
        for entry in entries:
            for rep in entry["representatives"]:
                merged.setdefault(tuple(rep), rep)
        detail = [item for entry in entries
                  for item in entry["unrepresented_detail"]]
        axes[name] = {
            "keys": len(merged),
            "length": sum(entry["length"] for entry in entries),
            "representatives": list(merged.values()),
            "unrepresented": len(detail),
            "unrepresented_detail": detail,
            "oversampled": sum(entry["oversampled"] for entry in entries),
            "opaque": any(entry["opaque"] for entry in entries),
            "candidates": sum(entry["candidates"] for entry in entries),
        }
    axes["config"] = {
        "keys": 1, "length": 1, "representatives": [config],
        "unrepresented": 0, "unrepresented_detail": [], "oversampled": 0,
        "opaque": False,
        "candidates": 1,
        "note": "run once per config; the corpus config axis is the profile",
    }
    opcodes = sorted({opcode for builder in result.builders
                      for opcode in builder.opcodes})
    axes["opcode"] = {
        "keys": len(opcodes), "length": len(opcodes),
        "representatives": opcodes, "unrepresented": 0,
        "unrepresented_detail": [], "oversampled": 0,
        "opaque": False, "candidates": len(opcodes),
    }
    total_rules = sum(len(builder.rules) for builder in result.builders)
    return {
        "schema": "llvm-legalizer-quotient-report",
        "version": 1,
        "target": profile.name,
        "config": config,
        "classes": [E.llt_name(t) for t in classes],
        "mem_classes": [E.llt_name(t) for t in mem_classes],
        "aligns": aligns,
        "axes": axes,
        "coverage": {
            "rules_total": total_rules,
            "rules_with_representative": covered,
            "rules_without_representative": uncovered,
        },
        "opaque_builders": sorted(opaque_builders),
        "per_builder": per_builder,
        "notes": [
            "A key is the tuple of modelled predicate results (1/0/None) for "
            "one dependency component; None is a distinct opaque value.",
            "The candidate universe is the parsed class list, so class counts "
            "are exact for modelled predicates, not a panel sample.",
            "Predicate calls inside lambdas are not recovered; rule sets with "
            "lambdas are listed in opaque_builders and keep the floor of the "
            "modelled quotient.",
            "unrepresented counts quotient classes the target boundary panel "
            "never touches; unrepresented_detail gives one example and a "
            "reason per class (panel_gap or opaque_predicate).  It is a "
            "panel-coverage diagnostic, not a gate.",
        ],
    }
