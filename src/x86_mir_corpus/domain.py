"""Turn a parsed rule ledger into deterministic query points.

A point is a query shape with no expected answer.  The C++ driver fills the
answer from the frozen reference; this module never does.
"""

import itertools
import json
import os

from . import dsl as _dsl
from . import expr as E
from . import targets as target_profiles
from .lexer import matching
from .parse import eval_int, parse_args, predicate_slot_roles

KIND_RANK = {"p": 0, "s": 1, "i": 2, "f": 3, "v": 4, "nxv": 5}


def sorted_llts(values):
    def key(t):
        return (KIND_RANK.get(t[0], 9), E.llt_bits(t), E.llt_lanes(t), t)
    return sorted(set(values), key=key)


def point(rule_set, opcode, origin, types, mem=None, align=0, rule_index=None):
    if mem is None:
        align = 0
    return {
        "id": None,  # assigned after sorting
        "rule_set": rule_set,
        "opcode": opcode,
        "origin": origin,
        "rule_index": rule_index,
        "types": [E.llt_name(t) for t in types],
        "mem": E.llt_name(mem) if mem is not None else None,
        "align": int(align) if mem is not None else 0,
    }


def align_classes(aligns):
    # class 0 is "below every threshold"; class N is threshold N-1.
    thresholds = sorted(int(a) for a in aligns)
    return [0, *thresholds]


def rep_class(classes):
    # The reviewed non-panel slot representative is s32.
    for t in classes:
        if t == E.scalar(32):
            return t
    for t in classes:
        if t[0] in ("s", "i"):
            return t
    return classes[0]


_OPCODE_ARITY = None


def opcode_type_slots(opcode):
    """Number of type operands the generic opcode declares."""
    global _OPCODE_ARITY
    if _OPCODE_ARITY is None:
        _OPCODE_ARITY = _dsl.opcode_arity()
    entry = _OPCODE_ARITY.get(opcode)
    return entry[0] if entry else 0


def fill_type(kind, types):
    """One reviewed opcode fill, derived from the current type vector."""
    base = types[0]
    lanes = base[1] if E.is_vec(base) else None
    if kind == "same0":
        return base
    if kind == "i1of0":
        if not lanes:
            return E.scalar(1)
        return (E.scalable_vec(lanes, E.scalar(1)) if E.is_scalable(base)
                else E.vec(lanes, E.scalar(1)))
    if kind == "elem0":
        return base[2] if E.is_vec(base) else base
    if kind == "i1vec0":
        if not lanes:
            return E.scalar(1)
        return (E.scalable_vec(lanes, E.scalar(1)) if E.is_scalable(base)
                else E.vec(lanes, E.scalar(1)))
    if kind == "f32shape0":
        if not lanes:
            return E.float_(32, "ieee")
        elem = E.float_(32, "ieee")
        return (E.scalable_vec(lanes, elem) if E.is_scalable(base)
                else E.vec(lanes, elem))
    if kind == "operand1":
        return base if E.is_vec(base) else E.vec(4, E.scalar(32))
    if kind == "vec4s32":
        return E.vec(4, E.scalar(32))
    if kind == "s64":
        return E.scalar(64)
    raise ValueError(f"unknown type fill {kind!r}")


def apply_type_fills(opcode, types, profile=None, used_slots=None):
    """Extend *types* to the opcode's declared arity with reviewed fills.

    ``used_slots`` marks slots the rule actually constrains.  A slot outside it
    was only a search placeholder (the probe pads with s32) and is replaced by
    the opcode's fill, not preserved.  Without ``used_slots`` only missing
    trailing slots are appended.
    """
    fills = getattr(profile, "type_fills", None) or {}
    spec = fills.get(opcode)
    if not spec or not types:
        return list(types)
    arity = opcode_type_slots(opcode)
    out = list(types)
    for idx in range(len(out), arity):
        kind = spec.get(idx)
        if kind is None:
            return list(types)
        out.append(fill_type(kind, out))
    if used_slots is not None:
        for idx, kind in spec.items():
            if idx < len(out) and idx not in used_slots:
                out[idx] = fill_type(kind, out)
    return out


def probe_slot_count(builder, opcode, profile=None):
    """Type slots a probe should carry for *opcode*.

    X86 keeps the historical "largest slot a rule mentions" rule so its
    corpus stays byte-identical.  Targets that opt in use the opcode's
    declared arity, which is what represents a real ``LegalityQuery``.
    """
    slots = builder.num_slots
    if profile is not None and getattr(profile, "probe_opcode_arity", False):
        slots = max(slots, opcode_type_slots(opcode))
    return slots


# The reference applies the first matching rule's mutation.  A mutation that
# rebuilds a type element by element calls LLT::changeElementSize, which
# rejects pointer and pointer-vector types with an assertion.  Such a query is
# not a well-formed legalizer query: prune it instead of asking the reference
# to answer it.  This is a safety filter, not an oracle: it never produces an
# expected action.
# Only these can name a pointer as the mutated type.  clampScalar/minScalar/
# maxScalar/widenScalarToNextPow2 all predicate on isScalar(), so a pointer can
# never match them; widenScalarOrEltToNextPow2 needs a non-power-of-two scalar
# size and pointer sizes are powers of two.
ELEMENT_SIZE_SLOT0 = {
    "scalarSameSizeAs", "clampScalarOrElt", "minScalarOrElt", "maxScalarOrElt",
}


def is_pointer_like(t):
    return t[0] == "p" or (E.is_vec(t) and t[2][0] == "p")


def element_size_slots(builder):
    """Slots an element-size mutation may target, conservatively."""
    slots = set()
    for rule in builder.rules:
        method = rule.method
        if method in ELEMENT_SIZE_SLOT0:
            idx = eval_int(rule.args[0]) if rule.args else None
            if idx is None:
                slots.update(range(builder.num_slots))
            else:
                slots.add(idx)
        elif method == "widenScalarIf":
            # The predicate is opaque; any slot it reads may be the target.
            slots.update(range(builder.num_slots))
    return slots


def pointer_unsafe(types, unsafe):
    return any(i in unsafe and is_pointer_like(t) for i, t in enumerate(types))


def class_pair(builder, classes, mem_classes, aligns):
    out = []
    pruned = 0
    rep = rep_class(classes)
    unsafe = element_size_slots(builder)
    for opcode in builder.opcodes:
        if builder.has_mem:
            mems = sorted_llts(mem_classes) or [rep]
            for value in classes:
                for other in classes:
                    if pointer_unsafe([value, other], unsafe):
                        pruned += len(mems) * len(align_classes(aligns))
                        continue
                    for mem in mems:
                        for align in align_classes(aligns):
                            out.append(point(builder.name, opcode, "classpair",
                                             [value, other], mem, align))
        elif builder.num_slots == 1:
            for ty in classes:
                if pointer_unsafe([ty], unsafe):
                    pruned += 1
                    continue
                out.append(point(builder.name, opcode, "classpair", [ty]))
        elif builder.num_slots >= 3:
            # A 3-slot query has no affordable full cube.  Vary one slot and
            # then each slot pair against the representative; rule probes fill
            # the remaining slots.
            base = [rep] * builder.num_slots
            for i in range(builder.num_slots):
                for ty in classes:
                    row = list(base)
                    row[i] = ty
                    if pointer_unsafe(row, unsafe):
                        pruned += 1
                        continue
                    out.append(point(builder.name, opcode, "classpair", row))
            for i in range(builder.num_slots):
                for j in range(i + 1, builder.num_slots):
                    for ty in classes:
                        row = list(base)
                        row[i] = row[j] = ty
                        if pointer_unsafe(row, unsafe):
                            pruned += 1
                            continue
                        out.append(point(builder.name, opcode, "classpair",
                                         row))
        else:
            for left in classes:
                for right in classes:
                    if pointer_unsafe([left, right], unsafe):
                        pruned += 1
                        continue
                    out.append(point(builder.name, opcode, "classpair",
                                     [left, right]))
    return out, pruned


def boundary(builder, classes, panel, mode):
    out = []
    pruned = 0
    rep = rep_class(classes)
    align = align_classes(builder.aligns)[1] if builder.aligns else 1
    unsafe = element_size_slots(builder)
    for opcode in builder.opcodes:
        if mode == "witness":
            for index, b in enumerate(panel):
                if builder.has_mem:
                    if index % 2 == 0:
                        out.append(point(builder.name, opcode, "boundary",
                                         [b, E.ptr(0)], rep, align))
                    else:
                        out.append(point(builder.name, opcode, "boundary",
                                         [rep, E.ptr(0)], b, align))
                elif builder.num_slots == 1:
                    out.append(point(builder.name, opcode, "boundary", [b]))
                elif builder.num_slots >= 3:
                    row = [rep] * builder.num_slots
                    row[index % builder.num_slots] = b
                    out.append(point(builder.name, opcode, "boundary", row))
                elif index % 2 == 0:
                    out.append(point(builder.name, opcode, "boundary", [b, rep]))
                else:
                    out.append(point(builder.name, opcode, "boundary", [rep, b]))
            continue
        # full/deep: both directions over the whole class axis.
        if builder.has_mem:
            for c in classes:
                if pointer_unsafe([c, E.ptr(0)], unsafe):
                    pruned += 2 * len(panel) * len(align_classes(builder.aligns))
                    continue
                for b in panel:
                    for al in align_classes(builder.aligns):
                        out.append(point(builder.name, opcode, "boundary",
                                         [c, E.ptr(0)], b, al))
                        out.append(point(builder.name, opcode, "boundary",
                                         [b, E.ptr(0)], c, al))
        elif builder.num_slots == 1:
            for b in panel:
                out.append(point(builder.name, opcode, "boundary", [b]))
        elif builder.num_slots >= 3:
            for i in range(builder.num_slots):
                for c in classes:
                    for b in panel:
                        row = [rep] * builder.num_slots
                        row[i] = c
                        row[(i + 1) % builder.num_slots] = b
                        out.append(point(builder.name, opcode, "boundary", row))
        else:
            for c in classes:
                for b in panel:
                    if pointer_unsafe([c, b], unsafe):
                        pruned += 1
                    else:
                        out.append(point(builder.name, opcode, "boundary", [c, b]))
                    if pointer_unsafe([b, c], unsafe):
                        pruned += 1
                    else:
                        out.append(point(builder.name, opcode, "boundary", [b, c]))
    return out, pruned


def address_space(builder, classes, skip_slot1_as=frozenset()):
    out = []
    p0, p1, p2 = E.ptr(0), E.ptr(1), E.ptr(2)
    align = align_classes(builder.aligns)[1] if builder.aligns else 1

    def emit(opcode, types, mem=None, al=0):
        name = builder.name
        if (name in skip_slot1_as and len(types) == 2
                and types[0] == p0 and types[1] in (p1, p2)
                and (mem is None or mem == p0)):
            return
        out.append(point(name, opcode, "as", types, mem, al))

    for opcode in builder.opcodes:
        if builder.has_mem:
            emit(opcode, [p1, p0], p0, align)
            emit(opcode, [p2, p0], p0, align)
            emit(opcode, [p0, p1], p0, align)
            emit(opcode, [p0, p2], p0, align)
            emit(opcode, [p1, p0], p1, align)
            emit(opcode, [p1, p0], p2, align)
            emit(opcode, [p2, p0], p1, align)
            emit(opcode, [p2, p0], p2, align)
            emit(opcode, [p0, p0], p1, align)
            emit(opcode, [p0, p0], p2, align)
        elif builder.num_slots == 1:
            emit(opcode, [p1])
            emit(opcode, [p2])
        elif builder.num_slots >= 3:
            base = [rep_class(classes)] * builder.num_slots
            emit(opcode, [p1, *base[1:]])
            emit(opcode, [p2, *base[1:]])
        else:
            emit(opcode, [p1, p0])
            emit(opcode, [p2, p0])
            emit(opcode, [p0, p1])
            emit(opcode, [p0, p2])
    return out


def opcode_probes(builder, classes, mem_classes):
    out = []
    if len(builder.opcodes) < 2:
        return out
    rep = rep_class(classes)
    mem = (sorted_llts(mem_classes) or [rep])[0]
    for opcode in builder.opcodes[1:]:
        if builder.has_mem:
            out.append(point(builder.name, opcode, "opcode",
                             [rep, E.ptr(0)], mem, 1))
        elif builder.num_slots == 1:
            out.append(point(builder.name, opcode, "opcode", [rep]))
        else:
            out.append(point(builder.name, opcode, "opcode",
                             [rep] * builder.num_slots))
    return out

# --- rule predicates and candidate probes ---------------------------------

def pow2(n):
    return n > 0 and (n & (n - 1)) == 0


def next_nonpow2(min_bits):
    bits = max(3, int(min_bits))
    while pow2(bits):
        bits += 1
    return bits


def next_pow2_at_least(bits):
    bits = max(1, int(bits))
    return 1 << (bits - 1).bit_length()


def choose_vec(classes, elem, accepted):
    for t in classes:
        if E.is_vec(t) and t[2] == elem and accepted(t[1]):
            return t
    return None


def synth_vec(elem, lanes):
    # LLT::vector rejects ElementCount(1); a one-element vector is a scalar.
    return E.vec(max(2, int(lanes)), elem)


def choose_scalar(classes, kind, accepted, fallback_bits):
    for t in classes:
        if t[0] == kind and accepted(t[1]):
            return t
    return E.scalar(max(1, int(fallback_bits)))


def first_list(values):
    for value in reversed(values):
        if isinstance(value, list) and value:
            return value
    return None


def probe_env(classes, features=None, llt_extended=None):
    env = {E.llt_name(t): t for t in classes}
    if features:
        env["__features__"] = features
        env.update(features)
        env["__pointer_bits__"] = 64 if features.get("is64Bit") else 32
        if llt_extended is None:
            llt_extended = features.get("lltExtended", True)
        env["__llt_extended__"] = bool(llt_extended)
    for order in ("NotAtomic", "Unordered", "Monotonic", "Acquire", "Release",
                  "AcquireRelease", "SequentiallyConsistent"):
        env[f"AtomicOrdering::{order}"] = order
    return env


def scalar_bits(ty, env):
    """LLT getScalarSizeInBits; pointers have the config's pointer size."""
    return E.llt_scalar_bits(ty, env.get("__pointer_bits__", 0))


def size_bits(ty, env):
    return E.llt_bits(ty, env.get("__pointer_bits__", 0))


def safe_eval(node, env):
    try:
        return E.eval_expr(node, env)
    except E.ExprError:
        return None


def int_arg(values, index):
    if index >= len(values):
        return None
    value = values[index]
    if isinstance(value, bool):
        return int(value)
    return value if isinstance(value, int) else None


def llt_arg(values, index):
    if index >= len(values) or not E.is_llt(values[index]):
        return None
    return values[index]


def item_to_probe(item):
    """Turn one type-list entry into (slot map, mem type, align)."""
    if E.is_llt(item):
        return ({0: item}, None, 0)
    if isinstance(item, list):
        if len(item) == 4 and E.is_llt(item[2]):
            return ({0: item[0], 1: item[1]}, item[2], int(item[3]))
        llts = [x for x in item if E.is_llt(x)]
        if llts:
            return ({i: t for i, t in enumerate(llts)}, None, 0)
    return None


def _arg_int(args, index, env):
    if index >= len(args):
        return None
    value = safe_eval(args[index], env)
    if isinstance(value, bool):
        return int(value)
    return value if isinstance(value, int) else None


def _arg_llt(args, index, env):
    if index >= len(args):
        return None
    value = safe_eval(args[index], env)
    return value if E.is_llt(value) else None


def _arg_list(args, index, env):
    if index >= len(args):
        return None
    value = safe_eval(args[index], env)
    return value if isinstance(value, list) else None


def _query_value(node, types, mem, env):
    """An LLT/int value read from the query, or None."""
    if isinstance(node, E.Call):
        if node.name.endswith(".MMODescrs.__index__.MemoryTy"):
            return mem
        if node.name.endswith(".MMODescrs.__index__"):
            idx = _arg_int(node.args, 0, env)
            return mem if idx == 0 else None
        if node.name.endswith(".Ordering"):
            # Corpus memory operands use the LLVM default ordering.
            return "NotAtomic"
        if node.name.endswith(".Types.__index__"):
            idx = _arg_int(node.args, 0, env)
            if idx is not None and 0 <= idx < len(types):
                return types[idx]
            return None
        if node.receiver is not None:
            base = _query_value(node.receiver, types, mem, env)
            if base is None:
                return None
            ctx = dict(env, __types__=types, __mem__=mem)
            try:
                return E.apply_member(node.name.rsplit(".", 1)[-1], base,
                                      node.args, ctx)
            except E.ExprError:
                return None
        if node.name.endswith(".__index__.MemoryTy"):
            return mem
        if node.name.endswith(".getSizeInBits"):
            inner = _query_value(E.Ident(node.name[:-len(".getSizeInBits")]),
                                 types, mem, env)
            return size_bits(inner, env) if E.is_llt(inner) else None
        if node.name.endswith(".getNumElements"):
            inner = _query_value(E.Ident(node.name[:-len(".getNumElements")]),
                                 types, mem, env)
            return E.llt_lanes(inner) if E.is_llt(inner) else None
        return None
    if isinstance(node, E.Ident):
        if node.name.endswith(".MemoryTy"):
            return mem
        value = env.get(node.name)
        if E.is_llt(value) or isinstance(value, (int, str, list, tuple)):
            return value
        return None
    if isinstance(node, E.Num):
        return node.value
    if isinstance(node, E.ListLit):
        out = []
        for item in node.items:
            value = _query_value(item, types, mem, env)
            if value is None:
                return None
            out.append(value)
        return out
    if isinstance(node, E.Binary):
        left = _query_value(node.left, types, mem, env)
        right = _query_value(node.right, types, mem, env)
        if isinstance(left, int) and isinstance(right, int):
            try:
                return E.apply_binary(node.op, left, right)
            except (E.ExprError, ZeroDivisionError):
                return None
        return None
    if isinstance(node, E.Unary) and node.op in ("-", "+", "~"):
        value = _query_value(node.x, types, mem, env)
        if isinstance(value, int):
            return {"-": -value, "+": value, "~": ~value}[node.op]
        return None
    return None


def _slot_at(types, idx):
    if idx is None or not (0 <= idx < len(types)):
        return None
    return types[idx]


def _size_or_none(ty, env):
    """getSizeInBits() as an int, or None when TypeSize cannot convert.

    ``LLT::getSizeInBits()`` is a TypeSize; the implicit conversion to a fixed
    integer aborts for scalable vectors.  A predicate that compares a scalable
    size is therefore not a query we can answer honestly.
    """
    if E.is_scalable(ty):
        return None
    return size_bits(ty, env)


def _mem_size_or_none(mem, env):
    if mem is None:
        return None
    return _size_or_none(mem, env)


PREDICATE_IMPL = {}
MUTATION_IMPL = {}


def predicate(name):
    """Register the implementation of one exact legality predicate."""
    def register(fn):
        PREDICATE_IMPL[name] = fn
        return fn
    return register


def mutation(name):
    """Register the implementation of one LegalizeMutation factory."""
    def register(fn):
        MUTATION_IMPL[name] = fn
        return fn
    return register


def _slot_ty(args, types, index, env):
    idx = _arg_int(args, index, env)
    return idx, _slot_at(types, idx)


@predicate("all")
def _pred_all(args, types, mem, align, env):
    out = True
    for arg in args:
        value = _pred_match(arg, types, mem, align, env)
        if value is None:
            return None
        out = out and value
    return out


@predicate("any")
def _pred_any(args, types, mem, align, env):
    out = False
    for arg in args:
        value = _pred_match(arg, types, mem, align, env)
        if value is None:
            return None
        out = out or value
    return out


@predicate("predNot")
def _pred_not(args, types, mem, align, env):
    if len(args) != 1:
        return None
    value = _pred_match(args[0], types, mem, align, env)
    return None if value is None else not value


@predicate("typeIs")
def _pred_type_is(args, types, mem, align, env):
    _, got = _slot_ty(args, types, 0, env)
    want = _arg_llt(args, 1, env)
    if got is None or want is None:
        return None
    return got == want


@predicate("typeIsNot")
def _pred_type_is_not(args, types, mem, align, env):
    _, got = _slot_ty(args, types, 0, env)
    want = _arg_llt(args, 1, env)
    if got is None or want is None:
        return None
    return got != want


@predicate("typeInSet")
def _pred_type_in_set(args, types, mem, align, env):
    idx = _arg_int(args, 0, env)
    values = _arg_list(args, 1, env)
    if idx is None or values is None:
        return None
    ty = _slot_at(types, idx)
    return None if ty is None else ty in values


@predicate("typePairInSet")
def _pred_type_pair_in_set(args, types, mem, align, env):
    idx0 = _arg_int(args, 0, env)
    idx1 = _arg_int(args, 1, env)
    values = _arg_list(args, 2, env)
    if idx0 is None or idx1 is None or values is None:
        return None
    if not (0 <= idx0 < len(types) and 0 <= idx1 < len(types)):
        return None
    return any(isinstance(pair, list) and len(pair) >= 2
               and types[idx0] == pair[0] and types[idx1] == pair[1]
               for pair in values)


@predicate("typeTupleInSet")
def _pred_type_tuple_in_set(args, types, mem, align, env):
    idx0 = _arg_int(args, 0, env)
    idx1 = _arg_int(args, 1, env)
    idx2 = _arg_int(args, 2, env)
    values = _arg_list(args, 3, env)
    if None in (idx0, idx1, idx2) or values is None:
        return None
    if not all(0 <= i < len(types) for i in (idx0, idx1, idx2)):
        return None
    return any(isinstance(tup, list) and len(tup) >= 3
               and types[idx0] == tup[0] and types[idx1] == tup[1]
               and types[idx2] == tup[2] for tup in values)


@predicate("typePairAndMemDescInSet")
def _pred_type_pair_and_mem_desc_in_set(args, types, mem, align, env):
    idx0 = _arg_int(args, 0, env)
    idx1 = _arg_int(args, 1, env)
    values = _arg_list(args, 3, env)
    if idx0 is None or idx1 is None or values is None or mem is None:
        return None
    if not (0 <= idx0 < len(types) and 0 <= idx1 < len(types)):
        return None
    for entry in values:
        if not (isinstance(entry, list) and len(entry) >= 4):
            continue
        ty0, ty1, mem_ty, mem_align = entry[:4]
        if (types[idx0] == ty0 and types[idx1] == ty1
                and E.llt_bits(mem) == E.llt_bits(mem_ty)
                and align >= int(mem_align)):
            return True
    return False


@predicate("scalarNarrowerThan")
def _pred_scalar_narrower_than(args, types, mem, align, env):
    idx = _arg_int(args, 0, env)
    bits = _arg_int(args, 1, env)
    ty = _slot_at(types, idx)
    if ty is None or bits is None:
        return None
    # C++: QueryTy.isScalar() && QueryTy.getSizeInBits() < Size.  The scalar
    # test short-circuits before the TypeSize conversion.
    if not E.is_scalar(ty):
        return False
    size = _size_or_none(ty, env)
    return None if size is None else size < bits


@predicate("scalarWiderThan")
def _pred_scalar_wider_than(args, types, mem, align, env):
    idx = _arg_int(args, 0, env)
    bits = _arg_int(args, 1, env)
    ty = _slot_at(types, idx)
    if ty is None or bits is None:
        return None
    if not E.is_scalar(ty):
        return False
    size = _size_or_none(ty, env)
    return None if size is None else size > bits


@predicate("scalarOrEltNarrowerThan")
def _pred_scalar_or_elt_narrower_than(args, types, mem, align, env):
    idx = _arg_int(args, 0, env)
    bits = _arg_int(args, 1, env)
    ty = _slot_at(types, idx)
    if ty is None or bits is None:
        return None
    return scalar_bits(ty, env) < bits


@predicate("scalarOrEltWiderThan")
def _pred_scalar_or_elt_wider_than(args, types, mem, align, env):
    idx = _arg_int(args, 0, env)
    bits = _arg_int(args, 1, env)
    ty = _slot_at(types, idx)
    if ty is None or bits is None:
        return None
    return scalar_bits(ty, env) > bits


@predicate("scalarOrEltSizeNotPow2")
def _pred_scalar_or_elt_size_not_pow2(args, types, mem, align, env):
    idx = _arg_int(args, 0, env)
    ty = _slot_at(types, idx)
    return None if ty is None else not pow2(scalar_bits(ty, env))


@predicate("sizeNotPow2")
def _pred_size_not_pow2(args, types, mem, align, env):
    idx = _arg_int(args, 0, env)
    ty = _slot_at(types, idx)
    if ty is None:
        return None
    if not E.is_scalar(ty):
        return False
    size = _size_or_none(ty, env)
    return None if size is None else not pow2(size)


@predicate("sizeNotMultipleOf")
def _pred_size_not_multiple_of(args, types, mem, align, env):
    idx = _arg_int(args, 0, env)
    size_arg = _arg_int(args, 1, env)
    ty = _slot_at(types, idx)
    if ty is None or size_arg is None:
        return None
    size = _size_or_none(ty, env)
    if size is None:
        return None
    return E.is_scalar(ty) and size % size_arg != 0


@predicate("sizeIs")
def _pred_size_is(args, types, mem, align, env):
    idx = _arg_int(args, 0, env)
    size_arg = _arg_int(args, 1, env)
    ty = _slot_at(types, idx)
    if ty is None or size_arg is None:
        return None
    size = _size_or_none(ty, env)
    return None if size is None else size == size_arg


@predicate("sameSize")
def _pred_same_size(args, types, mem, align, env):
    idx0 = _arg_int(args, 0, env)
    idx1 = _arg_int(args, 1, env)
    ty0 = _slot_at(types, idx0)
    ty1 = _slot_at(types, idx1)
    if ty0 is None or ty1 is None:
        return None
    left = _size_or_none(ty0, env)
    right = _size_or_none(ty1, env)
    if left is None or right is None:
        return None
    return left == right


@predicate("smallerThan")
def _pred_smaller_than(args, types, mem, align, env):
    idx0 = _arg_int(args, 0, env)
    idx1 = _arg_int(args, 1, env)
    ty0 = _slot_at(types, idx0)
    ty1 = _slot_at(types, idx1)
    if ty0 is None or ty1 is None:
        return None
    left = _size_or_none(ty0, env)
    right = _size_or_none(ty1, env)
    if left is None or right is None:
        return None
    return left < right


@predicate("largerThan")
def _pred_larger_than(args, types, mem, align, env):
    idx0 = _arg_int(args, 0, env)
    idx1 = _arg_int(args, 1, env)
    ty0 = _slot_at(types, idx0)
    ty1 = _slot_at(types, idx1)
    if ty0 is None or ty1 is None:
        return None
    left = _size_or_none(ty0, env)
    right = _size_or_none(ty1, env)
    if left is None or right is None:
        return None
    return left > right


@predicate("isScalar")
def _pred_is_scalar(args, types, mem, align, env):
    ty = _slot_at(types, _arg_int(args, 0, env))
    return None if ty is None else E.is_scalar(ty)


@predicate("isVector")
def _pred_is_vector(args, types, mem, align, env):
    ty = _slot_at(types, _arg_int(args, 0, env))
    return None if ty is None else E.is_vec(ty)


@predicate("isPointer")
def _pred_is_pointer(args, types, mem, align, env):
    idx = _arg_int(args, 0, env)
    ty = _slot_at(types, idx)
    if ty is None:
        return None
    if not E.is_ptr(ty):
        return False
    if len(args) >= 2:
        addr_space = _arg_int(args, 1, env)
        return None if addr_space is None else ty[1] == addr_space
    return True


@predicate("isPointerVector")
def _pred_is_pointer_vector(args, types, mem, align, env):
    ty = _slot_at(types, _arg_int(args, 0, env))
    return None if ty is None else E.is_vec(ty) and E.is_ptr(ty[2])


@predicate("elementTypeIs")
def _pred_element_type_is(args, types, mem, align, env):
    idx = _arg_int(args, 0, env)
    elt = _arg_llt(args, 1, env)
    ty = _slot_at(types, idx)
    if ty is None or elt is None:
        return None
    return E.is_vec(ty) and ty[2] == elt


@predicate("numElementsNotPow2")
def _pred_num_elements_not_pow2(args, types, mem, align, env):
    ty = _slot_at(types, _arg_int(args, 0, env))
    if ty is None:
        return None
    return E.is_vec(ty) and not E.is_scalable(ty) and not pow2(ty[1])


@predicate("vectorElementCountIsGreaterThan")
def _pred_vector_element_count_gt(args, types, mem, align, env):
    idx = _arg_int(args, 0, env)
    bound = _arg_int(args, 1, env)
    ty = _slot_at(types, idx)
    if ty is None or bound is None:
        return None
    if not E.is_vec(ty) or E.is_scalable(ty):
        return False
    return ty[1] > bound


@predicate("vectorElementCountIsLessThanOrEqualTo")
def _pred_vector_element_count_le(args, types, mem, align, env):
    idx = _arg_int(args, 0, env)
    bound = _arg_int(args, 1, env)
    ty = _slot_at(types, idx)
    if ty is None or bound is None:
        return None
    if not E.is_vec(ty) or E.is_scalable(ty):
        return False
    return ty[1] <= bound


@predicate("memSizeInBytesNotPow2")
def _pred_mem_size_in_bytes_not_pow2(args, types, mem, align, env):
    size = _mem_size_or_none(mem, env)
    if size is None:
        return None
    return size == 0 or not pow2(size // 8)


@predicate("memSizeNotByteSizePow2")
def _pred_mem_size_not_byte_size_pow2(args, types, mem, align, env):
    size = _mem_size_or_none(mem, env)
    if size is None:
        return None
    if size % 8 != 0:
        return True
    return size == 0 or not pow2(size // 8)


_ATOMIC_ORDER = {
    "NotAtomic": 0,
    "Unordered": 1,
    "Monotonic": 2,
    "Acquire": 3,
    "Release": 4,
    "AcquireRelease": 5,
    "SequentiallyConsistent": 6,
}


@predicate("atomicOrderingAtLeastOrStrongerThan")
def _pred_atomic_order_at_least(args, types, mem, align, env):
    # Corpus points do not carry an MMO ordering field; the sidecar builds
    # every memory operand with the LLVM default, AtomicOrdering::NotAtomic.
    # Modelling that value is therefore faithful to the executed MIR.
    if len(args) < 2:
        return None
    order = args[1]
    name = getattr(order, "name", None) or getattr(order, "text", "")
    name = str(name).rsplit("::", 1)[-1]
    if name not in _ATOMIC_ORDER:
        return None
    return _ATOMIC_ORDER["NotAtomic"] >= _ATOMIC_ORDER[name]


@predicate("immIs")
@predicate("immIsNot")
@predicate("immInSet")
def _pred_immediate_unknown(args, types, mem, align, env):
    # Query points do not carry immediates yet; stay undecided.
    return None


def _pred_match(node, types, mem, align, env):
    """True/False/None for the LLVM legality predicates.

    None means "the Python side does not model this predicate", which is how a
    first-match verdict stays honest instead of guessing.
    """
    # Bind the query values so chained LLT queries can be evaluated.
    if env.get("__types__") is not types:
        env = dict(env, __types__=types, __mem__=mem)
    if isinstance(node, E.Call):
        name = node.name.rsplit("::", 1)[-1]
        impl = PREDICATE_IMPL.get(name)
        if impl is not None:
            return impl(node.args, types, mem, align, env)
        if name in ("LegalityPredicate", "std::function") and node.args:
            inner = node.args[0]
            if isinstance(inner, E.Lambda):
                return lambda_bool(inner, types, mem, align, env)
            return _pred_match(inner, types, mem, align, env)
        if name == "is_contained" and len(node.args) >= 2:
            container = _query_value(node.args[0], types, mem, env)
            item = _query_value(node.args[1], types, mem, env)
            if isinstance(container, (list, tuple)) and item is not None:
                return item in container
            return None
        # Lambda locals hold LLT values; ``SrcTy.isVector()`` and friends are
        # LLT methods, not DSL predicates.
        try:
            value = E.eval_expr(node, env)
        except E.ExprError:
            return None
        return value if isinstance(value, bool) else None
    if isinstance(node, E.Binary):
        if node.op in ("==", "!="):
            left = _query_value(node.left, types, mem, env)
            right = _query_value(node.right, types, mem, env)
            if left is not None and right is not None:
                return (left == right) if node.op == "==" else (left != right)
        if node.op in ("<", ">", "<=", ">="):
            left = _query_value(node.left, types, mem, env)
            right = _query_value(node.right, types, mem, env)
            if isinstance(left, int) and isinstance(right, int):
                return {
                    "<": left < right, ">": left > right,
                    "<=": left <= right, ">=": left >= right,
                }[node.op]
        value = _query_value(node, types, mem, env)
        if isinstance(value, int) and not isinstance(value, bool):
            return bool(value)
        left = _pred_match(node.left, types, mem, align, env)
        if left is None:
            return None
        if node.op == "&&":
            return left and _pred_match(node.right, types, mem, align, env)
        if node.op == "||":
            return left or _pred_match(node.right, types, mem, align, env)
        return None
    if isinstance(node, E.Unary) and node.op == "!":
        value = _pred_match(node.x, types, mem, align, env)
        return None if value is None else not value
    if isinstance(node, E.Ternary):
        cond = _pred_match(node.cond, types, mem, align, env)
        if cond is None:
            return None
        return _pred_match(node.then if cond else node.other,
                           types, mem, align, env)
    if isinstance(node, E.Ident):
        name = node.name.rsplit("::", 1)[-1]
        if name in ("always", "true"):
            return True
        if name in ("never", "false"):
            return False
        value = env.get(node.name)
        if isinstance(value, bool):
            return value
        try:
            value = E.eval_expr(node, env)
        except E.ExprError:
            return None
        return value if isinstance(value, bool) else None
    if isinstance(node, E.Num):
        return bool(node.value)
    return None


# --- mutations --------------------------------------------------------------
# Semantics mirror llvm/lib/CodeGen/GlobalISel/LegalizeMutations.cpp at the
# pinned revision.  ``apply_mutation`` returns ``(TypeIdx, LLT)`` or None when
# the parsed mutation is opaque or the reference would assert.

def _mutation_type_idx(mut):
    idx = mut.get("type_idx")
    return idx if isinstance(idx, int) and not isinstance(idx, bool) else None


def _mutation_slot(mut):
    target = mut.get("target") or {}
    if target.get("kind") != "slot":
        return None
    idx = target.get("type_idx")
    return idx if isinstance(idx, int) and not isinstance(idx, bool) else None


def _mutation_target_llt(mut, types, env):
    target = mut.get("target") or {}
    kind = target.get("kind")
    if kind == "llt":
        try:
            return E.parse_llt_name(target.get("llt"))
        except E.ExprError:
            return None
    if kind == "slot":
        return _slot_at(types, _mutation_slot(mut))
    return None


def _mutation_size(mut):
    target = mut.get("target") or {}
    if target.get("kind") != "size":
        return None
    bits = target.get("bits")
    return bits if isinstance(bits, int) and not isinstance(bits, bool) else None


def _change_element_type(ty, new_elem):
    if not E.is_vec(ty):
        return new_elem
    factory = E.scalable_vec if E.is_scalable(ty) else E.vec
    return factory(ty[1], new_elem)


def _change_element_size(ty, size):
    if E.is_ptr(ty):
        raise ValueError("changeElementSize on a pointer LLT")
    if E.is_vec(ty):
        factory = E.scalable_vec if E.is_scalable(ty) else E.vec
        return factory(ty[1], _change_element_size(ty[2], size))
    if ty[0] == "i":
        return E.integer(size)
    if ty[0] == "f" and ty[2] == "ieee":
        return E.float_(size)
    return E.scalar(size)


def _change_element_count(ty, ec):
    kind, count = ec[1], ec[2]
    elem = E.elem_type(ty) if E.is_vec(ty) else ty
    if kind == "fixed" and count == 1:
        return elem
    factory = E.scalable_vec if kind == "scalable" else E.vec
    return factory(count, elem)


@mutation("changeTo")
def _mut_change_to(mut, types, env):
    idx = _mutation_type_idx(mut)
    target = _mutation_target_llt(mut, types, env)
    if idx is None or target is None:
        return None
    return (idx, target)


@mutation("changeElementTo")
def _mut_change_element_to(mut, types, env):
    idx = _mutation_type_idx(mut)
    if idx is None or not (0 <= idx < len(types)):
        return None
    target = _mutation_target_llt(mut, types, env)
    if target is None:
        return None
    return (idx, _change_element_type(types[idx], target))


@mutation("changeElementSizeTo")
def _mut_change_element_size_to(mut, types, env):
    idx = _mutation_type_idx(mut)
    if idx is None or not (0 <= idx < len(types)):
        return None
    target = _mutation_target_llt(mut, types, env)
    if target is None:
        return None
    size = E.llt_scalar_bits(target, env.get("__pointer_bits__", 0))
    try:
        return (idx, _change_element_size(types[idx], size))
    except ValueError:
        return None


@mutation("changeElementCountTo")
def _mut_change_element_count_to(mut, types, env):
    idx = _mutation_type_idx(mut)
    if idx is None or not (0 <= idx < len(types)):
        return None
    target = mut.get("target") or {}
    kind = target.get("kind")
    if kind == "element_count":
        ec = E.element_count(
            "scalable" if target.get("scalable") else "fixed",
            target.get("count"))
    elif kind == "slot":
        source = _slot_at(types, _mutation_slot(mut))
        if source is None:
            return None
        ec = (E.element_count("scalable", source[1]) if E.is_scalable(source)
              else E.element_count("fixed", source[1] if E.is_vec(source) else 1))
    else:
        return None
    return (idx, _change_element_count(types[idx], ec))


@mutation("widenScalarOrEltToNextPow2")
def _mut_widen_scalar_or_elt_to_next_pow2(mut, types, env):
    idx = _mutation_type_idx(mut)
    if idx is None or not (0 <= idx < len(types)):
        return None
    ty = types[idx]
    bits = E.llt_scalar_bits(ty, env.get("__pointer_bits__", 0))
    widest = max(next_pow2_at_least(bits), _mutation_size(mut) or 0)
    try:
        return (idx, _change_element_size(ty, widest))
    except ValueError:
        return None


@mutation("widenScalarOrEltToNextMultipleOf")
def _mut_widen_scalar_or_elt_to_next_multiple_of(mut, types, env):
    idx = _mutation_type_idx(mut)
    size = _mutation_size(mut)
    if idx is None or not (0 <= idx < len(types)) or not size:
        return None
    ty = types[idx]
    bits = E.llt_scalar_bits(ty, env.get("__pointer_bits__", 0))
    widest = ((bits + size - 1) // size) * size
    try:
        return (idx, _change_element_size(ty, widest))
    except ValueError:
        return None


@mutation("moreElementsToNextPow2")
def _mut_more_elements_to_next_pow2(mut, types, env):
    idx = _mutation_type_idx(mut)
    if idx is None or not (0 <= idx < len(types)):
        return None
    ty = types[idx]
    # LegalizeMutations.cpp calls getNumElements(), which is fatal for a
    # scalable vector; such a query is not well formed.
    if not E.is_vec(ty) or E.is_scalable(ty):
        return None
    lanes = max(next_pow2_at_least(ty[1]), _mutation_size(mut) or 0)
    return (idx, E.vec(lanes, ty[2]))


@mutation("scalarize")
def _mut_scalarize(mut, types, env):
    idx = _mutation_type_idx(mut)
    if idx is None or not (0 <= idx < len(types)):
        return None
    ty = types[idx]
    if not E.is_vec(ty):
        return None
    return (idx, ty[2])


def apply_mutation(mut, types, env=None):
    """Apply a parsed ``Rule.mutation``; return ``(TypeIdx, LLT)`` or None."""
    if not mut or mut.get("kind") != "call":
        return None
    impl = MUTATION_IMPL.get(mut.get("name"))
    if impl is None:
        return None
    try:
        return impl(mut, types, env or {})
    except (E.ExprError, KeyError, TypeError):
        return None


def lambda_expr(lam):
    """The final return expression of a simple lambda, or None.

    Lambdas with statements (switch, multiple returns) are opaque to this
    reader and stay undecided.
    """
    if lam is None or any(t.text in ("switch", "if") for t in lam.body):
        return None
    body = lam.body
    for i, tok in enumerate(body):
        if tok.text != "return":
            continue
        j = i + 1
        depth = 0
        while j < len(body):
            text = body[j].text
            if text in ("(", "[", "{"):
                depth += 1
            elif text in (")", "]", "}"):
                if depth == 0:
                    break
                depth -= 1
            elif text == ";" and depth == 0:
                break
            j += 1
        try:
            return E.Parser(body, i + 1, j).parse_expr()
        except E.ExprError:
            return None
    return None


def list_matches(entries, types, mem, align):
    for item in entries or []:
        if E.is_llt(item):
            if types and types[0] == item:
                return True
            continue
        if not isinstance(item, list):
            continue
        if len(item) == 4 and E.is_llt(item[2]):
            if (len(types) >= 2 and types[0] == item[0] and types[1] == item[1]
                    and mem is not None and align >= int(item[3])
                    and E.llt_bits(mem) == E.llt_bits(item[2])):
                return True
            continue
        llts = [x for x in item if E.is_llt(x)]
        # legalFor({{a,b}, ...}) is typePairInSet(0, 1, ...): it only compares
        # the named type indices, even when the opcode has more operand types
        # (G_VECREDUCE_FADD has three).
        if len(llts) == 2:
            if len(types) >= 2 and types[0] == llts[0] and types[1] == llts[1]:
                return True
            continue
        if len(llts) == 3:
            if (len(types) >= 3 and types[0] == llts[0]
                    and types[1] == llts[1] and types[2] == llts[2]):
                return True
            continue
        if len(llts) == len(types) and all(
                a == b for a, b in zip(llts, types, strict=True)):
            return True
    return False


def cartesian_matches(lists, types):
    # actionForCartesianProduct: one list means "types 0 and 1 are both in
    # this list"; two/three lists map to their type index respectively.
    if len(lists) == 1:
        if not types:
            return False
        if len(types) == 1:
            return types[0] in lists[0]
        return types[0] in lists[0] and types[1] in lists[0]
    if len(lists) > len(types):
        return False
    return all(types[i] in lst for i, lst in enumerate(lists))


def args_without_feature_gate(rule):
    """Arguments after an optional leading ``HasFeature`` predicate.

    ``minScalar(HasCSSC, 0, s32)`` is the gated overload of
    ``minScalar(0, s32)``; the feature value is already reflected by
    ``Rule.active``.  ``Rule.active`` uses the same convention.
    """
    args = list(rule.args)
    values = list(rule.evaluated_args)
    if values and isinstance(values[0], bool) and len(args) > 1:
        return args[1:], values[1:]
    return args, values


class _OpaqueLambda(Exception):
    """The statement subset in a lambda body is not modelled."""


_NO_RETURN = object()


def _lambda_bool_value(node, types, mem, align, env):
    value = _pred_match(node, types, mem, align, env)
    if value is not None:
        return value
    try:
        value = E.eval_expr(node, env)
    except E.ExprError:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    return None


def _statement_span(toks, start, end):
    """(statement_end, next_statement_start) for one C++ statement."""
    if start >= end:
        return end, end
    if toks[start].text == "{":
        close = matching(toks, start, end)
        if close < 0:
            raise _OpaqueLambda()
        return close + 1, close + 1
    depth = 0
    i = start
    while i < end:
        text = toks[i].text
        if text in ("(", "[", "{"):
            depth += 1
        elif text in (")", "]", "}"):
            if depth == 0:
                break
            depth -= 1
        elif text == ";" and depth == 0:
            return i + 1, i + 1
        i += 1
    raise _OpaqueLambda()


def _lambda_exec_block(toks, start, end, types, mem, align, env, opcode):
    """Execute the modelled subset of a lambda body; ``_NO_RETURN`` if none."""
    i = start
    while i < end:
        text = toks[i].text
        if text == ";":
            i += 1
            continue
        if text in ("switch", "for", "while", "do", "goto"):
            raise _OpaqueLambda()
        if text == "{":
            close = matching(toks, i, end)
            if close < 0:
                raise _OpaqueLambda()
            value = _lambda_exec_block(toks, i + 1, close, types, mem, align,
                                       env, opcode)
            if value is not _NO_RETURN:
                return value
            i = close + 1
            continue
        if text == "return":
            j = i + 1
            while j < end and toks[j].text != ";":
                j += 1
            if j >= end:
                raise _OpaqueLambda()
            node = E.Parser(toks, i + 1, j).parse_expr()
            value = _lambda_bool_value(node, types, mem, align, env)
            if value is None:
                raise _OpaqueLambda()
            return value
        if text == "if":
            if i + 1 >= end or toks[i + 1].text != "(":
                raise _OpaqueLambda()
            close = matching(toks, i + 1, end)
            if close < 0:
                raise _OpaqueLambda()
            node = E.Parser(toks, i + 2, close).parse_expr()
            cond = _lambda_bool_value(node, types, mem, align, env)
            if cond is None:
                raise _OpaqueLambda()
            then_start = close + 1
            then_end, after = _statement_span(toks, then_start, end)
            if cond:
                value = _lambda_exec_block(toks, then_start, then_end, types,
                                           mem, align, env, opcode)
                if value is not _NO_RETURN:
                    return value
            i = after
            if i < end and toks[i].text == "else":
                else_start = i + 1
                else_end, after_else = _statement_span(toks, else_start, end)
                if not cond:
                    value = _lambda_exec_block(toks, else_start, else_end,
                                               types, mem, align, env, opcode)
                    if value is not _NO_RETURN:
                        return value
                i = after_else
            continue
        # Declaration or assignment: find ';' and a top-level '='.
        depth = 0
        semi = i
        while semi < end:
            part = toks[semi].text
            if part in ("(", "[", "{"):
                depth += 1
            elif part in (")", "]", "}"):
                if depth == 0:
                    raise _OpaqueLambda()
                depth -= 1
            elif part == ";" and depth == 0:
                break
            semi += 1
        if semi >= end:
            raise _OpaqueLambda()
        eq = -1
        depth = 0
        for k in range(i, semi):
            part = toks[k].text
            if part in ("(", "[", "{"):
                depth += 1
            elif part in (")", "]", "}"):
                depth -= 1
            elif part == "=" and depth == 0:
                eq = k
        if eq > i:
            name_tok = toks[eq - 1]
            if name_tok.kind != "id":
                raise _OpaqueLambda()
            node = E.Parser(toks, eq + 1, semi).parse_expr()
            try:
                value = E.eval_expr(node, env)
            except E.ExprError:
                value = _query_value(node, types, mem, env)
            if value is None:
                raise _OpaqueLambda()
            env[name_tok.text] = value
        i = semi + 1
    return _NO_RETURN


def lambda_bool(lam, types, mem, align, env, opcode=None):
    """True/False/None for a predicate lambda, including simple statements."""
    if lam is None:
        return None
    expr = lambda_expr(lam)
    local = dict(env)
    local.update(lambda_local_env(lam, opcode, types, mem, env))
    local["__types__"] = types
    local["__mem__"] = mem
    if expr is not None:
        return _pred_match(expr, types, mem, align, local)
    try:
        value = _lambda_exec_block(lam.body, 0, len(lam.body), types, mem,
                                   align, local, opcode)
    except _OpaqueLambda:
        return None
    if value is _NO_RETURN or not isinstance(value, bool):
        return None
    return value


def predicate_value(node, types, mem, align, env, opcode=None):
    """Evaluate a predicate argument, resolving inline or named lambdas."""
    while isinstance(node, E.Call) and node.name.rsplit("::", 1)[-1] in (
            "LegalityPredicate", "std::function"):
        if not node.args:
            return None
        node = node.args[0]
    if isinstance(node, E.Lambda):
        return lambda_bool(node, types, mem, align, env, opcode)
    return _pred_match(node, types, mem, align, env)



def rule_matches(rule, types, mem, align, env, opcode=None):
    state = rule.active
    if state is False:
        return False
    if state is None:
        return None
    method = rule.method
    pred_args, a = args_without_feature_gate(rule)
    if method in ("legalFor", "customFor", "lowerFor", "libcallFor",
                  "legalForTypesWithMemDesc"):
        entries = first_list(a)
        if entries is None:
            return None
        return list_matches(entries, types, mem, align)
    if method in ("legalForCartesianProduct", "customForCartesianProduct",
                  "lowerForCartesianProduct", "libcallForCartesianProduct"):
        lists = [v for v in a if isinstance(v, list)]
        if not lists:
            return None
        return cartesian_matches(lists, types)
    if method in ("alwaysLegal", "lower", "libcall", "custom", "unsupported"):
        return True
    if method in ("clampScalar", "clampScalarOrElt"):
        slot, lo, hi = int_arg(a, 0), llt_arg(a, 1), llt_arg(a, 2)
        if slot is None or lo is None or hi is None or not (0 <= slot < len(types)):
            return None
        ty = types[slot]
        if method == "clampScalar" and not E.is_scalar(ty):
            return False
        return (scalar_bits(ty, env) < scalar_bits(lo, env)
                or scalar_bits(ty, env) > scalar_bits(hi, env))
    if method in ("minScalar", "maxScalar"):
        slot, bound = int_arg(a, 0), llt_arg(a, 1)
        if slot is None or bound is None or not (0 <= slot < len(types)):
            return None
        ty = types[slot]
        if not E.is_scalar(ty):
            return False
        size = _size_or_none(ty, env)
        if size is None:
            return None
        if method == "minScalar":
            return size < E.llt_bits(bound, env.get("__pointer_bits__", 0))
        return size > E.llt_bits(bound, env.get("__pointer_bits__", 0))
    if method in ("minScalarSameAs", "maxScalarSameAs"):
        slot, other = int_arg(a, 0), int_arg(a, 1)
        if slot is None or other is None:
            return None
        if not (0 <= slot < len(types) and 0 <= other < len(types)):
            return None
        other_bits = scalar_bits(types[other], env)
        own_bits = size_bits(types[slot], env)
        if method == "minScalarSameAs":
            return other_bits > own_bits
        return other_bits < own_bits
    if method in ("widenScalarToNextPow2", "widenScalarOrEltToNextPow2"):
        slot = int_arg(a, 0)
        if slot is None or not (0 <= slot < len(types)):
            return None
        ty = types[slot]
        if method == "widenScalarToNextPow2":
            size = _size_or_none(ty, env)
            return None if size is None else E.is_scalar(ty) and not pow2(size)
        return not pow2(scalar_bits(ty, env))
    if method == "clampNumElements":
        slot, lo, hi = int_arg(a, 0), llt_arg(a, 1), llt_arg(a, 2)
        if slot is None or lo is None or hi is None or not (0 <= slot < len(types)):
            return None
        ty = types[slot]
        return (E.is_vec(ty) and ty[2] == lo[2]
                and (ty[1] < lo[1] or ty[1] > hi[1]))
    if method in ("clampMinNumElements", "clampMaxNumElements"):
        slot, elem, count = int_arg(a, 0), llt_arg(a, 1), int_arg(a, 2)
        if slot is None or elem is None or count is None or not (0 <= slot < len(types)):
            return None
        ty = types[slot]
        if not E.is_vec(ty) or ty[2] != elem:
            return False
        return ty[1] < count if method == "clampMinNumElements" else ty[1] > count
    if method == "moreElementsToNextPow2":
        slot = int_arg(a, 0)
        if slot is None or not (0 <= slot < len(types)):
            return None
        return E.is_vec(types[slot]) and not pow2(types[slot][1])
    if method == "scalarize":
        slot = int_arg(a, 0)
        if slot is None or not (0 <= slot < len(types)):
            return None
        return E.is_vec(types[slot])
    if method == "scalarSameSizeAs":
        idx0, idx1 = int_arg(a, 0), int_arg(a, 1)
        if idx0 is None or idx1 is None:
            return None
        if not (0 <= idx0 < len(types) and 0 <= idx1 < len(types)):
            return None
        return scalar_bits(types[idx0], env) != scalar_bits(types[idx1], env)
    if method == "widenScalarIf":
        if not pred_args:
            return None
        return predicate_value(pred_args[0], types, mem, align, env, opcode)
    if method in ("customIf", "legalIf", "lowerIf", "unsupportedIf",
                  "narrowScalarIf", "moreElementsIf", "fewerElementsIf",
                  "bitcastIf"):
        if not pred_args:
            return None
        return predicate_value(pred_args[0], types, mem, align, env, opcode)
    if method == "scalarizeIf":
        slot = int_arg(a, 1)
        if slot is None or not (0 <= slot < len(types)):
            return None
        if not pred_args:
            return None
        predicate = predicate_value(pred_args[0], types, mem, align, env,
                                    opcode)
        return None if predicate is None else (
            predicate and E.is_vec(types[slot]))
    if method == "widenVectorEltsToVectorMinSize":
        slot, min_size = int_arg(a, 0), int_arg(a, 1)
        if slot is None or min_size is None or not (0 <= slot < len(types)):
            return None
        ty = types[slot]
        if not E.is_vec(ty) or ty[0] != "v":
            return False
        return size_bits(ty, env) < min_size
    if method == "widenScalarOrEltToNextPow2OrMinSize":
        slot = int_arg(a, 0)
        min_size = int_arg(a, 1) or 0
        if slot is None or not (0 <= slot < len(types)):
            return None
        ty = types[slot]
        width = scalar_bits(ty, env)
        return width < min_size or not pow2(width)
    if method in ("unsupportedIfMemSizeNotPow2", "lowerIfMemSizeNotPow2",
                  "lowerIfMemSizeNotByteSizePow2"):
        if mem is None:
            return None
        pred = E.Call(
            "memSizeNotByteSizePow2" if
            method == "lowerIfMemSizeNotByteSizePow2"
            else "memSizeInBytesNotPow2", [])
        return _pred_match(pred, types, mem, align, env)
    if method in ("minScalarOrElt", "minScalarOrEltIf",
                  "minScalarIf", "maxScalarIf"):
        base = 1 if method in ("minScalarOrEltIf", "minScalarIf",
                               "maxScalarIf") else 0
        slot, bound = int_arg(a, base), llt_arg(a, base + 1)
        if slot is None or bound is None or not (0 <= slot < len(types)):
            return None
        predicate = True
        if base == 1:
            if not pred_args:
                return None
            predicate = predicate_value(pred_args[0], types, mem, align, env,
                                        opcode)
            if predicate is None:
                return None
        width = scalar_bits(types[slot], env)
        bound_bits = E.llt_bits(bound, env.get("__pointer_bits__", 0))
        if method == "maxScalarIf":
            return predicate and width > bound_bits
        return predicate and width < bound_bits
    if method in ("minScalarEltSameAsIf", "maxScalarEltSameAsIf"):
        small, large = int_arg(a, 1), int_arg(a, 2)
        if small is None or large is None:
            return None
        if not (0 <= small < len(types) and 0 <= large < len(types)):
            return None
        if not pred_args:
            return None
        predicate = predicate_value(pred_args[0], types, mem, align, env,
                                    opcode)
        if predicate is None:
            return None
        if method == "minScalarEltSameAsIf":
            return predicate and (scalar_bits(types[small], env)
                                  < scalar_bits(types[large], env))
        return predicate and (scalar_bits(types[small], env)
                              > scalar_bits(types[large], env))
    if method in ("widenScalarFor", "unsupportedFor"):
        if not a:
            return None
        entries = first_list(a)
        if entries is None:
            return None
        return list_matches(entries, types, mem, align)
    return None


def first_match(builder, types, mem, align, env, opcode=None):
    for rule in builder.rules:
        verdict = rule_matches(rule, types, mem, align, env, opcode)
        if verdict is None:
            return ("unknown", rule.index)
        if verdict:
            return ("match", rule.index)
    return ("none", None)


def pred_witness(node, classes, env=None):
    """A query shape that makes a simple predicate true, or None."""
    env = env or probe_env(classes)
    if not isinstance(node, E.Call):
        return None
    name, args = node.name, node.args
    if name in ("isVector", "isScalar", "isPointer", "isPointerVector"):
        idx = _arg_int(args, 0, env)
        if idx is None:
            return None
        if name == "isVector":
            return ({idx: E.vec(2, E.scalar(8))}, None, 0)
        if name == "isScalar":
            return ({idx: E.scalar(8)}, None, 0)
        if name == "isPointer":
            return ({idx: E.ptr(0)}, None, 0)
        return ({idx: E.vec(2, E.ptr(0))}, None, 0)
    if name in ("typeIs", "typeIsNot", "typeInSet"):
        idx = _arg_int(args, 0, env)
        if name == "typeInSet":
            values = _arg_list(args, 1, env)
            ty = next((v for v in values or [] if E.is_llt(v)), None)
        else:
            ty = _arg_llt(args, 1, env)
        if idx is None or ty is None:
            return None
        return ({idx: ty}, None, 0)
    if name in ("scalarNarrowerThan", "scalarOrEltNarrowerThan"):
        idx = _arg_int(args, 0, env)
        bits = _arg_int(args, 1, env)
        if idx is None or bits is None:
            return None
        return ({idx: E.scalar(max(1, next_pow2_at_least(bits) // 2))}, None, 0)
    if name in ("scalarWiderThan", "scalarOrEltWiderThan"):
        idx = _arg_int(args, 0, env)
        bits = _arg_int(args, 1, env)
        if idx is None or bits is None:
            return None
        return ({idx: E.scalar(next_pow2_at_least(bits + 1))}, None, 0)
    if name == "sizeNotPow2":
        idx = _arg_int(args, 0, env)
        if idx is None:
            return None
        return ({idx: E.scalar(3)}, None, 0)
    if name == "typePairInSet":
        idx0 = _arg_int(args, 0, env)
        idx1 = _arg_int(args, 1, env)
        values = _arg_list(args, 2, env)
        if idx0 is None or idx1 is None or not values:
            return None
        pair = next((p for p in values if isinstance(p, list) and len(p) >= 2), None)
        if pair is None:
            return None
        return ({idx0: pair[0], idx1: pair[1]}, None, 0)
    return None


def lambda_local_env(lam, opcode, types=None, mem=None, base_env=None):
    """Evaluate simple local assignments in a lambda body.

    X86_EXTRACT/G_INSERT compute SubIdx/FullIdx from Query.Opcode; resolving
    those locals is what makes a per-opcode legalIf witness possible.  When
    *types* is given, ``Query.Types[i]`` assignments also resolve, which is
    what lets an AArch64 ``customIf`` lambda be evaluated instead of staying
    opaque.
    """
    if lam is None:
        return {}
    env = dict(base_env or {})
    param = E.lambda_param_name(lam)
    env.update({
        "Query.Opcode": E.Opcode(opcode) if opcode else None,
        "G_EXTRACT": E.Opcode("G_EXTRACT"),
        "G_INSERT": E.Opcode("G_INSERT"),
        "Query.Types": None,
        f"{param}.Opcode": E.Opcode(opcode) if opcode else None,
        f"{param}.Types": None,
    })
    body = lam.body
    i = 0
    while i + 2 < len(body):
        if body[i].kind == "id" and body[i + 1].text == "=":
            j = i + 2
            depth = 0
            while j < len(body):
                text = body[j].text
                if text in ("(", "[", "{"):
                    depth += 1
                elif text in (")", "]", "}"):
                    depth -= 1
                elif text == ";" and depth == 0:
                    break
                j += 1
            try:
                node = E.Parser(body, i + 2, j).parse_expr()
            except E.ExprError:
                node = None
            value = None
            if node is not None and types is not None:
                value = _query_value(node, types, mem, env)
            if value is None and node is not None:
                try:
                    value = E.eval_expr(node, env)
                except E.ExprError:
                    value = None
            if value is not None:
                env[body[i].text] = value
            i = j + 1
            continue
        i += 1
    return env


def probe_from_lambda(lam, classes, rep, mem, opcode=None):
    """A best-effort query for an opaque lambda.

    Only literal predicate calls are used.  The coverage manifest marks these
    probes as unverified, so this is a useful shape rather than a claim about
    the lambda's full semantics.
    """
    if lam is None:
        return None
    env = probe_env(classes)
    env.update(lambda_local_env(lam, opcode))
    toks = lam.body
    i = 0
    while i < len(toks):
        tok = toks[i]
        if tok.text in ("typeIs", "typeIsNot", "typeInSet", "typePairInSet",
                        "scalarNarrowerThan", "scalarOrEltNarrowerThan",
                        "scalarWiderThan", "scalarOrEltWiderThan",
                        "sizeNotPow2") and i + 1 < len(toks) and toks[i + 1].text == "(":
            close = matching(toks, i + 1)
            if close > 0:
                args = parse_args(toks, i + 1, close, env)
                got = pred_witness(E.Call(tok.text, args), classes, env)
                if got is not None:
                    return got
                i = close + 1
                continue
        i += 1
    if any(t.text == "MMODescrs" for t in toks) and mem is not None:
        # X86_STORE's customIf: the value type differs from the memory type.
        for ty in classes:
            if ty != mem:
                return ({0: ty}, mem, 1)
    return None


def scalar_candidates(classes, kind="s"):
    bits = {t[1] for t in classes if t[0] == kind}
    extra = {1, 3, 7, 8, 16, 24, 32, 33, 64, 65, 80, 100, 128, 129, 200, 256}
    return [E.scalar(bits_) for bits_ in sorted(bits | extra)]


def vector_candidates(classes, elem):
    lanes = {t[1] for t in classes if E.is_vec(t) and t[2] == elem}
    extra = {2, 3, 4, 5, 8, 16, 32, 64, 65, 128}
    return [E.vec(n, elem) for n in sorted(lanes | extra)]


def all_vector_candidates(classes):
    elems = {t[2] for t in classes if E.is_vec(t)}
    elems.update({E.scalar(1), E.scalar(8), E.scalar(16), E.scalar(32),
                  E.scalar(64), E.ptr(0)})
    out = []
    for elem in sorted_llts(elems):
        out.extend(vector_candidates(classes, elem))
    return out


def predicate_slots(node, env=None, _depth=0):
    """Type slot indices a predicate argument reads."""
    env = env or {}
    out = set()
    if _depth > 12:
        return out
    if isinstance(node, E.Lambda):
        toks = node.body
        k = 0
        while k + 2 < len(toks):
            if (toks[k].text == "Types" and toks[k + 1].text == "["
                    and k + 2 < len(toks)):
                v = toks[k + 2]
                if v.kind == "num":
                    out.add(int(v.text))
                elif v.kind == "id":
                    idx = safe_eval(E.Ident(v.text), env)
                    if isinstance(idx, int):
                        out.add(idx)
                k += 3
                continue
            k += 1
        k = 0
        while k + 2 < len(toks):
            t = toks[k]
            if t.kind == "id" and toks[k + 1].text == "(":
                close = matching(toks, k + 1)
                if close < 0:
                    k += 1
                    continue
                try:
                    args = parse_args(toks, k + 1, close, env)
                except E.ExprError:
                    args = []
                for i in predicate_slot_roles(t.text, len(args)):
                    if i < len(args):
                        idx = safe_eval(args[i], env)
                        if isinstance(idx, int):
                            out.add(idx)
                k = close
            k += 1
        return out
    if isinstance(node, E.Call):
        name = node.name.rsplit("::", 1)[-1]
        for i in predicate_slot_roles(name, len(node.args)):
            if i < len(node.args):
                idx = safe_eval(node.args[i], env)
                if isinstance(idx, int):
                    out.add(idx)
        for arg in node.args:
            out |= predicate_slots(arg, env, _depth + 1)
        return out
    if isinstance(node, E.Binary):
        return (predicate_slots(node.left, env, _depth + 1)
                | predicate_slots(node.right, env, _depth + 1))
    if isinstance(node, E.Unary):
        return predicate_slots(node.x, env, _depth + 1)
    if isinstance(node, E.Ternary):
        return (predicate_slots(node.cond, env, _depth + 1)
                | predicate_slots(node.then, env, _depth + 1)
                | predicate_slots(node.other, env, _depth + 1))
    if isinstance(node, E.ListLit):
        for item in node.items:
            out |= predicate_slots(item, env, _depth + 1)
        return out
    return out


def generic_slot_pool(builder, classes, slot):
    """Representative types for a predicate-driven slot search."""
    out = set(classes)
    out.update(E.scalar(b) for b in (1, 3, 7, 8, 16, 32, 64, 128))
    for elem in (E.scalar(1), E.scalar(8), E.scalar(16), E.scalar(32),
                 E.scalar(64), E.ptr(0)):
        out.update(E.vec(lanes, elem) for lanes in (2, 3, 4, 8, 16))
    for elem in (("f", 16, "ieee"), ("f", 16, "bfloat"),
                 ("f", 32, "ieee"), ("f", 64, "ieee")):
        out.update(E.vec(lanes, elem) for lanes in (2, 4, 8))
    if builder.has_mem and slot == 1:
        out.add(E.ptr(0))
    return sorted_llts(out)


def generic_probe_candidates(rule, builder, classes, slot_count, env,
                             opcode=None, limit=6000):
    """Shapes where every predicate argument of *rule* evaluates true.

    This is the fallback for predicates that ``probe_candidates`` cannot
    derive syntactically (multi-statement lambdas, ``all(...)`` combinations,
    member-query comparisons).  It only proposes shapes; ``_search_rule_shape``
    still requires the rule to be the first match.
    """
    slots = set()
    for node in rule.args:
        if isinstance(node, (E.Lambda, E.Call, E.Ident)):
            slots |= predicate_slots(node, env)
    slots = {0} | {s for s in slots if 0 <= s < slot_count}
    slots = sorted(slots)
    if len(slots) > 2:
        # Keep the result slot and the first predicate slot; a full product is
        # the P4 shape-space problem, not a single-rule witness.
        extra = [s for s in slots if s != 0][:1]
        slots = [0, *extra]
    pools = [generic_slot_pool(builder, classes, s) for s in slots]
    base = [E.scalar(32)] * slot_count
    out = []
    seen = set()
    for combo in itertools.product(*[p[:64] for p in pools]):
        types = list(base)
        for slot, ty in zip(slots, combo, strict=False):
            types[slot] = ty
        key = tuple(types)
        if key in seen:
            continue
        seen.add(key)
        verdict = rule_matches(rule, types, None, 0, env, opcode)
        # A memory-dependent predicate cannot be decided without an MMO; the
        # caller varies mem, so keep None candidates instead of dropping them.
        if verdict is not False:
            slots_map = {slot: ty for slot, ty in zip(slots, combo,
                                                     strict=False)}
            out.append((slots_map, None, 0, "decided"))
            if len(out) >= limit:
                break
    return out


def probe_candidates(builder, rule, classes, rep, mem, opcode=None):
    """Every candidate shape worth trying for *rule*.

    The caller keeps the first one whose first match is the rule itself.  This
    is a search over query shapes, not a guess about the rule's action.
    """
    if rule.active is False:
        return []
    method = rule.method
    _, a = args_without_feature_gate(rule)

    if method == "alwaysLegal":
        return [({}, None, 0, "decided")]

    if method in ("legalFor", "customFor", "lowerFor", "libcallFor",
                  "legalForTypesWithMemDesc"):
        entries = first_list(a)
        out = []
        for item in entries or []:
            got = item_to_probe(item)
            if got is not None:
                out.append((*got, "decided"))
        return out

    if method in ("legalForCartesianProduct", "customForCartesianProduct",
                  "lowerForCartesianProduct", "libcallForCartesianProduct"):
        lists = []
        for value in a:
            if not isinstance(value, list) or not value:
                continue
            llts = [x for x in value if E.is_llt(x)]
            if not llts:
                return []
            lists.append(llts)
        if not lists:
            return []
        out = []
        for combo in itertools.product(*lists):
            slots = {i: ty for i, ty in enumerate(combo)}
            if len(lists) == 1 and builder.num_slots >= 2:
                slots[1] = combo[0]
            out.append((slots, None, 0, "decided"))
            if len(out) >= 64:
                break
        return out

    if method == "clampNumElements":
        slot, lo, hi = int_arg(a, 0), llt_arg(a, 1), llt_arg(a, 2)
        if slot is None or lo is None or hi is None or lo[0] != "v":
            return []
        return [({slot: ty}, None, 0, "decided")
                for ty in vector_candidates(classes, lo[2])]

    if method in ("clampMinNumElements", "clampMaxNumElements"):
        slot, elem = int_arg(a, 0), llt_arg(a, 1)
        if slot is None or elem is None:
            return []
        return [({slot: ty}, None, 0, "decided")
                for ty in vector_candidates(classes, elem)]

    if method in ("clampScalar", "clampScalarOrElt", "minScalar", "maxScalar"):
        slot, lo = int_arg(a, 0), llt_arg(a, 1)
        if slot is None or lo is None:
            return []
        return [({slot: ty}, None, 0, "decided")
                for ty in scalar_candidates(classes, lo[0])]

    if method in ("minScalarSameAs", "maxScalarSameAs"):
        slot, other = int_arg(a, 0), int_arg(a, 1)
        if slot is None or other is None:
            return []
        out = []
        pool = list(scalar_candidates(classes)) + list(
            all_vector_candidates(classes))
        for left in pool:
            for right in pool:
                right_bits = E.llt_scalar_bits(right)
                left_bits = E.llt_bits(left)
                hit = ((method == "minScalarSameAs"
                        and right_bits > left_bits)
                       or (method == "maxScalarSameAs"
                           and right_bits < left_bits))
                if hit:
                    out.append(({slot: left, other: right}, None, 0,
                                "decided"))
                    if len(out) >= 128:
                        return out
        return out

    if method in ("widenScalarToNextPow2", "widenScalarOrEltToNextPow2"):
        slot = int_arg(a, 0)
        if slot is None:
            return []
        bits = {t[1] for t in scalar_candidates(classes) if not pow2(t[1])}
        return [({slot: E.scalar(b)}, None, 0, "decided")
                for b in sorted(bits)]

    if method == "moreElementsToNextPow2":
        slot = int_arg(a, 0) or 0
        return [({slot: ty}, None, 0, "decided")
                for ty in all_vector_candidates(classes)]

    if method == "scalarize":
        slot = int_arg(a, 0) or 0
        return [({slot: ty}, None, 0, "decided")
                for ty in all_vector_candidates(classes)]

    if method == "scalarizeIf":
        slot = int_arg(a, 1)
        if slot is None:
            return []
        got = pred_witness(rule.args[0], classes) if rule.args else None
        if got is None:
            lam = next((x for x in rule.args if isinstance(x, E.Lambda)), None)
            got = probe_from_lambda(lam, classes, rep, mem, opcode)
        if got is None:
            return []
        slots, memdesc, align = got
        ty = next(iter(slots.values()), None)
        if ty is None:
            return []
        out = []
        base = ty if E.is_vec(ty) else E.vec(2, ty)
        out.append(({slot: base}, memdesc, align, "decided"))
        for lanes in (2, 3, 4, 8, 16, 32, 64, 128):
            if E.is_vec(base):
                out.append(({slot: E.vec(lanes, base[2])}, memdesc, align,
                            "decided"))
        return out

    if method == "scalarSameSizeAs":
        idx0, idx1 = int_arg(a, 0), int_arg(a, 1)
        if idx0 is None or idx1 is None:
            return []
        out = []
        for left in scalar_candidates(classes):
            for right in scalar_candidates(classes):
                if left[1] != right[1]:
                    out.append(({idx0: left, idx1: right}, None, 0, "decided"))
        return out

    if method in ("lower", "libcall"):
        return [({}, None, 0, "decided")]

    if method == "widenScalarIf":
        if not rule.args:
            return []
        got = pred_witness(rule.args[0], classes)
        if got is not None:
            return [(*got, "decided")]
        lam = next((x for x in rule.args if isinstance(x, E.Lambda)), None)
        got = probe_from_lambda(lam, classes, rep, mem, opcode)
        return [(*got, "opaque")] if got is not None else []

    if method in ("customIf", "legalIf", "lowerIf", "narrowScalarIf",
                  "moreElementsIf", "fewerElementsIf", "bitcastIf"):
        lam = next((x for x in rule.args if isinstance(x, E.Lambda)), None)
        got = (probe_from_lambda(lam, classes, rep, mem, opcode)
               if lam is not None else
               pred_witness(rule.args[0], classes) if rule.args else None)
        return [(*got, "opaque")] if got is not None else []

    if method == "widenVectorEltsToVectorMinSize":
        slot, min_size = int_arg(a, 0), int_arg(a, 1)
        if slot is None or min_size is None:
            return []
        return [({slot: ty}, None, 0, "decided")
                for ty in all_vector_candidates(classes)
                if ty[0] == "v" and E.llt_bits(ty) < min_size]

    if method == "widenScalarOrEltToNextPow2OrMinSize":
        slot = int_arg(a, 0)
        min_size = int_arg(a, 1) or 0
        if slot is None:
            return []
        out = []
        for ty in list(scalar_candidates(classes)) + list(
                all_vector_candidates(classes)):
            width = E.llt_scalar_bits(ty)
            if width < min_size or not pow2(width):
                out.append(({slot: ty}, None, 0, "decided"))
        return out

    if method in ("unsupportedIfMemSizeNotPow2", "lowerIfMemSizeNotPow2",
                  "lowerIfMemSizeNotByteSizePow2"):
        out = []
        mem_pool = set(builder.mem_classes)
        # A predicate about non-power-of-two memory sizes needs a boundary
        # representative even when the reviewed memory axis has none.
        mem_pool.update(E.scalar(bits) for bits in
                        (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 128, 192,
                         256))
        for mem_ty in sorted_llts(mem_pool):
            if E.llt_bits(mem_ty) % 8:
                continue
            bytes_ = E.llt_bits(mem_ty) // 8
            bad = (not pow2(bytes_)) if method == "lowerIfMemSizeNotByteSizePow2" \
                else (not pow2(max(1, bytes_)))
            if bad:
                out.append(({}, mem_ty, max(1, bytes_), "decided"))
        return out

    if method in ("minScalarOrElt", "minScalarOrEltIf", "minScalarIf",
                  "maxScalarIf"):
        base = 1 if method in ("minScalarOrEltIf", "minScalarIf",
                               "maxScalarIf") else 0
        slot, bound = int_arg(a, base), llt_arg(a, base + 1)
        if slot is None or bound is None:
            return []
        bound_bits = E.llt_bits(bound)
        out = []
        for ty in list(scalar_candidates(classes)) + list(
                all_vector_candidates(classes)):
            width = E.llt_scalar_bits(ty)
            hit = (width > bound_bits if method == "maxScalarIf"
                   else width < bound_bits)
            if hit:
                out.append(({slot: ty}, None, 0, "decided"))
        if base == 1 and rule.args:
            got = (pred_witness(rule.args[0], classes)
                   if not isinstance(rule.args[0], E.Lambda)
                   else probe_from_lambda(rule.args[0], classes, rep, mem,
                                          opcode))
            if got is None:
                # The generic predicate search will supply the shape.
                return out
            slots, memdesc, align = got
            out = [({**slots, **probe}, memdesc, align, "decided")
                   for probe, _, _, _ in out]
        return out

    if method in ("minScalarEltSameAsIf", "maxScalarEltSameAsIf"):
        small, large = int_arg(a, 1), int_arg(a, 2)
        if small is None or large is None:
            return []
        out = []
        for left in scalar_candidates(classes):
            for right in scalar_candidates(classes):
                if method == "minScalarEltSameAsIf":
                    hit = left[1] < right[1]
                else:
                    hit = left[1] > right[1]
                if hit:
                    out.append(({small: left, large: right}, None, 0,
                                "decided"))
        return out

    if method in ("widenScalarFor", "unsupportedFor"):
        entries = first_list(a)
        out = []
        for item in entries or []:
            got = item_to_probe(item)
            if got is not None:
                out.append((*got, "decided"))
        return out

    return []


def rule_issue(builder, rule, reason, first=None, kind=None):
    return {
        "rule_set": builder.name,
        "rule_index": rule.index,
        "method": rule.method,
        "arg": rule.arg_text[0] if rule.arg_text else "",
        "reason": reason,
        "kind": kind or "unknown",
        "first_match": first,
    }


def filler_pool(builder, classes, slot):
    if builder.has_mem and slot == 1:
        pool = [E.ptr(0), E.ptr(1), E.ptr(2)]
        pool += [t for t in classes if E.is_vec(t) and t[2][0] == "p"]
        return sorted_llts(set(pool))
    pool = list(classes)
    pool += [E.scalar(b) for b in (1, 3, 7, 24, 33, 65, 129, 200)]
    pool += [E.vec(2, E.scalar(64)), E.vec(3, E.scalar(32)),
             E.vec(5, E.scalar(8)), E.vec(3, E.ptr(0))]
    return sorted_llts(set(pool))


def types_variants(base, target_slots, builder, classes):
    """Keep the rule's own slots and vary the filler slots."""
    yield base
    fillers = [i for i in range(len(base)) if i not in target_slots]
    for i in fillers:
        for value in filler_pool(builder, classes, i):
            if value != base[i]:
                variant = list(base)
                variant[i] = value
                yield variant


def _search_rule_shape(builder, rule, classes, mem_classes, rep, mem, env,
                       opcode=None, slot_count=None, generic=False):
    """Search one opcode's shapes for a verdict where *rule* is the first match."""
    slots_total = max(1, slot_count or builder.num_slots)
    needs_mem = "MemoryTy" in (rule.arg_text[0] if rule.arg_text else "")
    declared = probe_candidates(builder, rule, classes, rep, mem, opcode)

    def scan(candidates, state):
        shadow, unknown, self_false, self_unknown, evaluated = state
        for slots, memdesc, align, confidence in candidates:
            base = [rep] * slots_total
            for i, ty in slots.items():
                if 0 <= i < len(base):
                    base[i] = ty
            if builder.has_mem:
                base = base[:2]
                while len(base) < 2:
                    base.append(E.ptr(0))
                probe_mem = memdesc if memdesc is not None else mem
                probe_align = align if memdesc is not None else 1
            else:
                probe_mem = None
                probe_align = 0
            mem_variants = [probe_mem]
            if builder.has_mem and memdesc is None:
                mem_pool = [m for m in [probe_mem, *list(mem_classes), base[0]]
                            if m is not None]
                if needs_mem:
                    # The memory type axis is part of the predicate signature;
                    # an extending/index load can require a narrower memory
                    # type than the reviewed mem-classes list contains.
                    mem_pool += [E.scalar(bits) for bits in
                                 (8, 16, 32, 64, 128)]
                mem_variants = sorted_llts(set(mem_pool)) or [probe_mem]
            for candidate_mem in mem_variants:
                for types in types_variants(base, set(slots), builder, classes):
                    evaluated = True
                    own = rule_matches(rule, types, candidate_mem,
                                       probe_align, env, opcode)
                    if own is True:
                        self_false = False
                    elif own is None:
                        self_unknown = True
                    verdict, first = first_match(builder, types, candidate_mem,
                                                 probe_align, env, opcode)
                    if verdict == "match" and first == rule.index:
                        return ((types, candidate_mem, probe_align, confidence),
                                state)
                    if verdict == "unknown" and unknown is None:
                        unknown = first
                    if verdict == "match":
                        shadow = first if shadow is None else max(shadow, first)
        return None, (shadow, unknown, self_false, self_unknown, evaluated)

    state = (None, None, True, False, False)
    got, state = scan(declared, state)
    if got is not None:
        return got, None
    if generic:
        extra = generic_probe_candidates(rule, builder, classes, slots_total,
                                         env, opcode)
        if extra:
            got, state = scan(extra, state)
            if got is not None:
                return got, None
    if not state[4]:
        return None, ("no candidate shape", None, "no_candidate")
    shadow, unknown, self_false, self_unknown, _ = state
    if self_false and not self_unknown:
        return None, ("predicate false for every candidate in this configuration",
                      None, "predicate_false")
    if unknown is not None:
        return None, (f"first match undecided because rule {unknown} is opaque",
                      unknown, "undecided")
    if shadow is not None:
        return None, (f"every candidate is shadowed by rule {shadow}",
                      shadow, "shadowed")
    return None, ("no candidate satisfies this rule in this configuration",
                  None, "no_candidate")


def rule_probes(builder, classes, mem_classes, env, profile=None):
    out = []
    issues = []
    rep = rep_class(classes)
    mem = (sorted_llts(mem_classes) or [rep])[0]
    opcodes = list(builder.opcodes)
    env = dict(env)
    env.update(getattr(builder, "predicate_locals", None) or {})
    env.update(getattr(builder, "locals", None) or {})
    for rule in builder.rules:
        if rule.active is False:
            continue
        chosen = {}
        diagnoses = {}
        for opcode in opcodes:
            got, diagnosis = _search_rule_shape(
                builder, rule, classes, mem_classes, rep, mem, env, opcode,
                probe_slot_count(builder, opcode, profile),
                generic=bool(profile is not None
                             and getattr(profile, "probe_opcode_arity", False)))
            diagnoses[opcode] = diagnosis
            if got is not None:
                chosen[opcode] = got
        if chosen:
            excluded = getattr(profile, "rule_probe_exclusions", frozenset())
            for opcode, got in chosen.items():
                types, probe_mem, probe_align, _confidence = got
                key = (opcode, tuple(E.llt_name(t) for t in types))
                if key in excluded:
                    issues.append(rule_issue(
                        builder, rule,
                        "probe shape " + ", ".join(key[1]) + " is normalised "
                        "by the pre-legalizer combiner; the raw legalizer "
                        "query is not IR-reachable",
                        None, "pipeline_unreachable"))
                    continue
                used = {0}
                for node in rule.args:
                    used |= predicate_slots(node, env)
                types = apply_type_fills(opcode, list(types), profile,
                                         used_slots=used)
                out.append(point(builder.name, opcode, "rule", types,
                                 probe_mem, probe_align, rule.index))
            continue
        reason, first, kind = diagnoses[opcodes[0]]
        issues.append(rule_issue(builder, rule, reason, first, kind))
    return out, issues

# --- coverage --------------------------------------------------------------

def exception_matches(entry, issue):
    if entry.get("rule_set") != issue["rule_set"]:
        return False
    if "config" in entry and entry["config"] != issue.get("config"):
        return False
    if "configs" in entry and issue.get("config") not in entry["configs"]:
        return False
    if "rule_index" in entry and entry["rule_index"] != issue["rule_index"]:
        return False
    if entry.get("method") and entry["method"] != issue["method"]:
        return False
    if "arg" in entry and entry["arg"] != issue["arg"]:
        return False
    return not ("arg_contains" in entry
                and entry["arg_contains"] not in issue["arg"])


def load_coverage_exceptions(path=None):
    if path is None:
        package = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(package, "assets", "coverage_exceptions.json")
        if not os.path.exists(path):
            path = os.path.join(package, "coverage_exceptions.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return data.get("exceptions", [])


# Chain methods that call ``markAllIdxsAsCovered()`` in LegalizerInfo.h: they
# discharge the type/imm coverage verifier for the whole rule set.
COVER_ALL_METHODS = {
    "legalIf", "legalForTypeWithAnyImm", "alwaysLegal", "bitcastIf", "lower",
    "lowerIf", "libcall", "libcallIf", "custom", "customIf", "unsupported",
}


def construction_issues(result, table=None, profile=None):
    """Static checks mirrored from LegalizerInfo.cpp's verifier.

    These do not change point generation; they make shape gaps and invalid
    mutation/imm references visible with a stable ``detail_code`` instead of
    silently shrinking the corpus.
    """
    table = table or _dsl.load()
    arity = _dsl.opcode_arity(table)
    fills = getattr(profile, "type_fills", None) or {}
    unreachable = getattr(profile, "shape_arity_unreachable", None) or {}
    issues = []
    for builder in result.builders:
        known = [arity[op] for op in builder.opcodes if op in arity]
        expected_types = max((entry[0] for entry in known), default=None)
        expected_imms = max((entry[1] for entry in known), default=0)
        covers_all = any(rule.method in COVER_ALL_METHODS
                         for rule in builder.rules)
        # The opcode's dense type indices are authoritative; a bad mutation
        # must not widen the shape it is checked against.
        shape = expected_types if expected_types is not None \
            else builder.num_slots
        if not fills:
            if expected_types and expected_types != builder.num_slots:
                issues.append({
                    "rule_set": builder.name,
                    "rule_index": None,
                    "method": None,
                    "detail_code": "shape_arity",
                    "detail": (f"opcodes need {expected_types} type "
                               f"index(es); rules describe "
                               f"{builder.num_slots}"),
                })
        else:
            for opcode in builder.opcodes:
                entry = arity.get(opcode)
                if entry is None or entry[0] <= builder.num_slots:
                    continue
                spec = fills.get(opcode) or {}
                if all(idx in spec
                       for idx in range(builder.num_slots, entry[0])):
                    continue
                reason = unreachable.get(opcode)
                if reason:
                    issues.append({
                        "rule_set": builder.name,
                        "rule_index": None,
                        "method": None,
                        "opcode": opcode,
                        "detail_code": "shape_arity_unreachable",
                        "detail": reason,
                    })
                    continue
                issues.append({
                    "rule_set": builder.name,
                    "rule_index": None,
                    "method": None,
                    "opcode": opcode,
                    "detail_code": "shape_arity",
                    "detail": (f"{opcode} needs {entry[0]} type index(es); "
                               f"rules describe {builder.num_slots} and no "
                               "reviewed fill covers the rest"),
                })
        for rule in builder.rules:
            mutation = rule.mutation
            if mutation:
                type_idx = mutation.get("type_idx")
                if type_idx is not None and not 0 <= type_idx < shape:
                    issues.append({
                        "rule_set": builder.name,
                        "rule_index": rule.index,
                        "method": rule.method,
                        "detail_code": "mutation_type_idx",
                        "detail": (f"mutation targets type index {type_idx}; "
                                   f"shape has {shape}"),
                    })
                target = mutation.get("target") or {}
                if (target.get("kind") == "slot"
                        and not 0 <= target["type_idx"] < shape):
                    issues.append({
                        "rule_set": builder.name,
                        "rule_index": rule.index,
                        "method": rule.method,
                        "detail_code": "mutation_source_slot",
                        "detail": (f"mutation reads slot {target['type_idx']}; "
                                   f"shape has {shape}"),
                    })
            if rule.imm_idx is not None and (
                    rule.imm_idx < 0 or (expected_imms
                                         and rule.imm_idx >= expected_imms)):
                    issues.append({
                        "rule_set": builder.name,
                        "rule_index": rule.index,
                        "method": rule.method,
                        "detail_code": "imm_idx",
                        "detail": (f"immIdx({rule.imm_idx}); opcode has "
                                   f"{expected_imms} immediate index(es)"),
                    })
        if expected_imms and not covers_all:
            covered = {rule.imm_idx for rule in builder.rules
                       if rule.imm_idx is not None}
            if any(rule.method in ("legalForTypeWithAnyImm",
                                   "actionForTypeWithAnyImm")
                   for rule in builder.rules):
                covered.add(0)
            missing = sorted(set(range(expected_imms)) - covered)
            if missing:
                issues.append({
                    "rule_set": builder.name,
                    "rule_index": None,
                    "method": None,
                    "detail_code": "imm_coverage",
                    "detail": (f"opcode has {expected_imms} immediate "
                               f"index(es); uncovered: {missing}"),
                })
    return issues


def coverage_report(result, points, issues, inactive, exceptions,
                    construction=None):
    active_keys = set()
    for builder in result.builders:
        for rule in builder.rules:
            if rule.active is not False:
                active_keys.add((builder.name, rule.index))
    candidate_keys = {(p["rule_set"], p["rule_index"]) for p in points
                      if p["origin"] == "rule"}
    issue_keys = {(i["rule_set"], i["rule_index"]) for i in issues}
    excepted = []
    unexplained = []
    for issue in issues:
        issue = dict(issue)
        issue["config"] = result.config
        entry = next((e for e in exceptions if exception_matches(e, issue)), None)
        if entry is None:
            unexplained.append(issue)
        else:
            item = dict(issue)
            item["exception"] = entry.get("reason", "")
            excepted.append(item)

    opcodes = set()
    for builder in result.builders:
        opcodes.update(builder.opcodes)
    per_opcode = {}
    per_opcode_shapes = {}
    for p in points:
        counts = per_opcode.setdefault(p["rule_set"], {})
        counts[p["opcode"]] = counts.get(p["opcode"], 0) + 1
        if p["origin"] in ("rule", "boundary", "as"):
            shapes = per_opcode_shapes.setdefault(p["rule_set"], {})
            shapes[p["opcode"]] = shapes.get(p["opcode"], 0) + 1
    uncovered = []
    multi = {}
    shape_multi = {}
    for builder in result.builders:
        if len(builder.opcodes) < 2:
            continue
        multi[builder.name] = {
            opcode: per_opcode.get(builder.name, {}).get(opcode, 0)
            for opcode in builder.opcodes
        }
        shape_multi[builder.name] = {
            opcode: per_opcode_shapes.get(builder.name, {}).get(opcode, 0)
            for opcode in builder.opcodes
        }
        for opcode in builder.opcodes:
            if shape_multi[builder.name][opcode] == 0:
                uncovered.append({"rule_set": builder.name, "opcode": opcode})

    return {
        "policy": (
            "A rule entry is probed only when its candidate shape is the "
            "first matching rule under the modelled predicates. Opaque "
            "lambdas are reported as unverified candidates. Every active "
            "rule is either verified, listed in coverage_exceptions.json, or "
            "reported in rules.unprobed."),
        "rules": {
            "source_total": sum(len(b.rules) for b in result.builders),
            "active": len(active_keys),
            "verified": len(active_keys - issue_keys),
            "probed_candidates": len(active_keys & candidate_keys),
            "inactive": inactive,
            "excepted": excepted,
            "unprobed": unexplained,
        },
        "construction": {
            "issues": construction or [],
            "detail_codes": {
                code: sum(1 for issue in (construction or [])
                          if issue["detail_code"] == code)
                for code in sorted({issue["detail_code"]
                                    for issue in (construction or [])})
            },
        },
        "opcodes": {
            "total": len(opcodes),
            "multi_opcode_builders": len(multi),
            "secondary_opcodes": sum(len(b.opcodes) - 1
                                     for b in result.builders
                                     if len(b.opcodes) > 1),
            "per_opcode_atoms": multi,
            "per_opcode_shape_atoms": shape_multi,
            "uncovered": uncovered,
        },
    }


# --- top level -------------------------------------------------------------

def build_points(result, tier, exceptions=None, profile=None):
    profile = profile or target_profiles.get("x86")
    classes = sorted_llts(result.classes)
    mem_classes = sorted_llts(result.mem_classes)
    if tier == "witness":
        panel = profile.boundary_panel
        mode = "witness"
    elif tier == "deep":
        panel = profile.deep_panel
        mode = "full"
    elif tier == "full":
        panel = profile.boundary_panel
        mode = "full"
    else:
        raise ValueError(f"unknown tier {tier}")

    env = probe_env(classes, profile.feature_env(result.features),
                    profile.llt_extended)
    points = []
    issues = []
    inactive = []
    pruned = 0
    for builder in result.builders:
        for rule in builder.rules:
            if rule.active is False:
                inactive.append(rule_issue(
                    builder, rule, rule.why_inactive() or "inactive"))
        if tier in ("full", "deep"):
            built, skipped = class_pair(builder, classes, mem_classes,
                                        result.aligns)
            points.extend(built)
            pruned += skipped
        built, skipped = boundary(builder, classes, panel, mode)
        points.extend(built)
        pruned += skipped
        points.extend(address_space(builder, classes,
                                   profile.skip_witness_slot1_as))
        points.extend(opcode_probes(builder, classes, mem_classes))
        built, rule_issues = rule_probes(builder, classes, mem_classes, env,
                                         profile)
        points.extend(built)
        issues.extend(rule_issues)

    if getattr(profile, "type_fills", None):
        # Profile-gated so targets without reviewed fills keep byte-identical
        # points (the X86 full-C2 SHA is a frozen gate).
        for p in points:
            filled = apply_type_fills(
                p["opcode"], [E.parse_llt_name(name) for name in p["types"]],
                profile)
            p["types"] = [E.llt_name(t) for t in filled]
    construction = construction_issues(result, profile=profile)
    points.sort(key=point_sort_key)
    counters = {}
    for p in points:
        key = (p["origin"], p["rule_set"])
        counters[key] = counters.get(key, 0) + 1
        p["id"] = (f"cpp/{p['rule_set']}/{p['opcode']}/{p['origin']}/"
                   f"{counters[key] - 1}")
    coverage = coverage_report(result, points, issues, inactive,
                               exceptions or [], construction)
    return points, classes, mem_classes, panel, pruned, coverage


def point_sort_key(p):
    return (
        p["origin"],
        p["rule_set"],
        p["opcode"],
        -1 if p["rule_index"] is None else p["rule_index"],
        tuple(p["types"]),
        p["mem"] or "",
        p["align"],
    )
