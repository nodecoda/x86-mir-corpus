"""Turn a parsed rule ledger into deterministic query points.

A point is a query shape with no expected answer.  The C++ driver fills the
answer from the frozen reference; this module never does.
"""

import itertools
import json
import os

from . import expr as E
from . import targets as target_profiles
from .lexer import matching
from .parse import eval_int, parse_args

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
        if E.is_llt(value) or isinstance(value, int):
            return value
        return None
    if isinstance(node, E.Num):
        return node.value
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


def _pred_match(node, types, mem, align, env):
    """True/False/None for the LLVM legality predicates.

    None means "the Python side does not model this predicate", which is how a
    first-match verdict stays honest instead of guessing.
    """
    # Bind the query values so chained LLT queries can be evaluated.
    if env.get("__types__") is not types:
        env = dict(env, __types__=types, __mem__=mem)
    if isinstance(node, E.Call):
        name = node.name
        # ``::any(...)`` parses with the namespace prefix attached.
        name = name.rsplit("::", 1)[-1]
        args = node.args

        def slot_ty(index=0):
            idx = _arg_int(args, index, env)
            return idx, _slot_at(types, idx)

        if name == "all":
            out = True
            for arg in args:
                value = _pred_match(arg, types, mem, align, env)
                if value is None:
                    return None
                out = out and value
            return out
        if name == "any":
            out = False
            for arg in args:
                value = _pred_match(arg, types, mem, align, env)
                if value is None:
                    return None
                out = out or value
            return out
        if name == "predNot":
            if len(args) != 1:
                return None
            value = _pred_match(args[0], types, mem, align, env)
            return None if value is None else not value
        if name == "typeIs":
            got = slot_ty()
            return None if got[1] is None else got[1] == _arg_llt(args, 1, env)
        if name == "typeIsNot":
            got = slot_ty()
            return None if got[1] is None else got[1] != _arg_llt(args, 1, env)
        if name == "typeInSet":
            idx = _arg_int(args, 0, env)
            values = _arg_list(args, 1, env)
            if idx is None or values is None:
                return None
            ty = _slot_at(types, idx)
            return None if ty is None else ty in values
        if name in ("typePairInSet", "typePairIs"):
            idx0 = _arg_int(args, 0, env)
            idx1 = _arg_int(args, 1, env)
            if idx0 is None or idx1 is None:
                return None
            if not (0 <= idx0 < len(types) and 0 <= idx1 < len(types)):
                return None
            if name == "typePairIs":
                ty0 = _arg_llt(args, 2, env)
                ty1 = _arg_llt(args, 3, env)
                if ty0 is None or ty1 is None:
                    return None
                return types[idx0] == ty0 and types[idx1] == ty1
            values = _arg_list(args, 2, env)
            if values is None:
                return None
            return any(isinstance(pair, list) and len(pair) >= 2
                       and types[idx0] == pair[0] and types[idx1] == pair[1]
                       for pair in values)
        if name == "typeTupleInSet":
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
        if name == "typePairAndMemDescInSet":
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
        if name in ("scalarNarrowerThan", "scalarWiderThan"):
            idx = _arg_int(args, 0, env)
            bits = _arg_int(args, 1, env)
            ty = _slot_at(types, idx)
            if ty is None or bits is None:
                return None
            size = _size_or_none(ty, env)
            if size is None:
                return None
            if not E.is_scalar(ty):
                return False
            return size < bits if name == "scalarNarrowerThan" else size > bits
        if name in ("scalarOrEltNarrowerThan", "scalarOrEltWiderThan"):
            idx = _arg_int(args, 0, env)
            bits = _arg_int(args, 1, env)
            ty = _slot_at(types, idx)
            if ty is None or bits is None:
                return None
            size = scalar_bits(ty, env)
            return size < bits if name == "scalarOrEltNarrowerThan" else size > bits
        if name == "scalarOrEltSizeNotPow2":
            idx = _arg_int(args, 0, env)
            ty = _slot_at(types, idx)
            return None if ty is None else not pow2(scalar_bits(ty, env))
        if name == "sizeNotPow2":
            idx = _arg_int(args, 0, env)
            ty = _slot_at(types, idx)
            if ty is None:
                return None
            size = _size_or_none(ty, env)
            return None if size is None else E.is_scalar(ty) and not pow2(size)
        if name == "sizeNotMultipleOf":
            idx = _arg_int(args, 0, env)
            size_arg = _arg_int(args, 1, env)
            ty = _slot_at(types, idx)
            if ty is None or size_arg is None:
                return None
            size = _size_or_none(ty, env)
            if size is None:
                return None
            return E.is_scalar(ty) and size % size_arg != 0
        if name == "sizeIs":
            idx = _arg_int(args, 0, env)
            size_arg = _arg_int(args, 1, env)
            ty = _slot_at(types, idx)
            if ty is None or size_arg is None:
                return None
            size = _size_or_none(ty, env)
            return None if size is None else size == size_arg
        if name in ("sameSize", "smallerThan", "largerThan"):
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
            if name == "sameSize":
                return left == right
            return left < right if name == "smallerThan" else left > right
        if name == "isScalar":
            ty = _slot_at(types, _arg_int(args, 0, env))
            return None if ty is None else E.is_scalar(ty)
        if name == "isVector":
            ty = _slot_at(types, _arg_int(args, 0, env))
            return None if ty is None else E.is_vec(ty)
        if name == "isPointer":
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
        if name == "isPointerVector":
            ty = _slot_at(types, _arg_int(args, 0, env))
            return None if ty is None else E.is_vec(ty) and E.is_ptr(ty[2])
        if name == "elementTypeIs":
            idx = _arg_int(args, 0, env)
            elt = _arg_llt(args, 1, env)
            ty = _slot_at(types, idx)
            if ty is None or elt is None:
                return None
            return E.is_vec(ty) and ty[2] == elt
        if name == "numElementsNotPow2":
            ty = _slot_at(types, _arg_int(args, 0, env))
            if ty is None:
                return None
            return E.is_vec(ty) and not E.is_scalable(ty) and not pow2(ty[1])
        if name in ("vectorElementCountIsGreaterThan",
                    "vectorElementCountIsLessThanOrEqualTo"):
            idx = _arg_int(args, 0, env)
            bound = _arg_int(args, 1, env)
            ty = _slot_at(types, idx)
            if ty is None or bound is None:
                return None
            if not E.is_vec(ty) or E.is_scalable(ty):
                return False
            return ty[1] > bound if name.endswith("GreaterThan") else ty[1] <= bound
        if name in ("memSizeInBytesNotPow2", "memSizeNotByteSizePow2"):
            size = _mem_size_or_none(mem, env)
            if size is None:
                return None
            if name == "memSizeNotByteSizePow2" and size % 8 != 0:
                return True
            return size == 0 or not pow2(size // 8)
        if name in ("immIs", "immIsNot", "immInSet"):
            # Query points do not carry immediates yet; stay undecided.
            return None
        if name == "atomicOrderingAtLeastOrStrongerThan":
            return None
        if name == "always":
            return True
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
            if (isinstance(left, int) and isinstance(right, int)):
                return {
                    "<": left < right, ">": left > right,
                    "<=": left <= right, ">=": left >= right,
                }[node.op]
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
        return _pred_match(node.then if cond else node.other, types, mem, align, env)
    if isinstance(node, E.Ident):
        value = env.get(node.name)
        return value if isinstance(value, bool) else None
    if isinstance(node, E.Num):
        return bool(node.value)
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
        if len(llts) == len(types) and all(
                a == b for a, b in zip(llts, types, strict=True)):
            return True
    return False


def cartesian_matches(lists, types):
    if len(lists) > len(types):
        return False
    return all(types[i] in lst for i, lst in enumerate(lists))


def rule_matches(rule, types, mem, align, env, opcode=None):
    state = rule.active
    if state is False:
        return False
    if state is None:
        return None
    method = rule.method
    a = rule.evaluated_args
    if method in ("legalFor", "customFor", "lowerFor", "libcallFor",
                  "legalForTypesWithMemDesc"):
        entries = first_list(a)
        if entries is None:
            return None
        return list_matches(entries, types, mem, align)
    if method in ("legalForCartesianProduct", "customForCartesianProduct",
                  "lowerForCartesianProduct", "libcallForCartesianProduct"):
        lists = [v for v in a if isinstance(v, list)]
        if len(lists) < len(types):
            return None
        return cartesian_matches(lists, types)
    if method in ("alwaysLegal", "lower", "libcall", "custom"):
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
        if not a:
            return None
        return _pred_match(rule.args[0], types, mem, align, env)
    if method in ("customIf", "legalIf", "lowerIf"):
        lam = next((x for x in rule.args if isinstance(x, E.Lambda)), None)
        expr = lambda_expr(lam)
        if expr is None:
            return None
        local = dict(env)
        local.update(lambda_local_env(lam, opcode, types, mem, env))
        return _pred_match(expr, types, mem, align, local)
    if method == "scalarizeIf":
        return None
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


def probe_candidates(builder, rule, classes, rep, mem, opcode=None):
    """Every candidate shape worth trying for *rule*.

    The caller keeps the first one whose first match is the rule itself.  This
    is a search over query shapes, not a guess about the rule's action.
    """
    if rule.active is False:
        return []
    method = rule.method
    a = rule.evaluated_args

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
            out.append(({i: ty for i, ty in enumerate(combo)}, None, 0,
                        "decided"))
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

    if method in ("customIf", "legalIf", "lowerIf"):
        lam = next((x for x in rule.args if isinstance(x, E.Lambda)), None)
        got = probe_from_lambda(lam, classes, rep, mem, opcode)
        return [(*got, "opaque")] if got is not None else []

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
                       opcode=None):
    """Search one opcode's shapes for a verdict where *rule* is the first match."""
    candidates = probe_candidates(builder, rule, classes, rep, mem, opcode)
    if not candidates:
        return None, ("no candidate shape", None, "no_candidate")
    shadow = None
    unknown = None
    self_false = True
    self_unknown = False
    for slots, memdesc, align, confidence in candidates:
        base = [rep] * max(1, builder.num_slots)
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
            mem_variants = sorted_llts(set(
                [probe_mem, *list(mem_classes), base[0]]))
        for candidate_mem in mem_variants:
            for types in types_variants(base, set(slots), builder, classes):
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
                            None)
                if verdict == "unknown" and unknown is None:
                    unknown = first
                if verdict == "match":
                    shadow = first if shadow is None else max(shadow, first)
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


def rule_probes(builder, classes, mem_classes, env):
    out = []
    issues = []
    rep = rep_class(classes)
    mem = (sorted_llts(mem_classes) or [rep])[0]
    opcodes = list(builder.opcodes)
    env = dict(env)
    env.update(getattr(builder, "locals", None) or {})
    for rule in builder.rules:
        if rule.active is False:
            continue
        chosen = {}
        diagnoses = {}
        for opcode in opcodes:
            got, diagnosis = _search_rule_shape(builder, rule, classes,
                                                mem_classes, rep, mem, env,
                                                opcode)
            diagnoses[opcode] = diagnosis
            if got is not None:
                chosen[opcode] = got
        if chosen:
            for opcode, got in chosen.items():
                types, probe_mem, probe_align, _confidence = got
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


def coverage_report(result, points, issues, inactive, exceptions):
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
        built, rule_issues = rule_probes(builder, classes, mem_classes, env)
        points.extend(built)
        issues.extend(rule_issues)

    points.sort(key=point_sort_key)
    counters = {}
    for p in points:
        key = (p["origin"], p["rule_set"])
        counters[key] = counters.get(key, 0) + 1
        p["id"] = (f"cpp/{p['rule_set']}/{p['opcode']}/{p['origin']}/"
                   f"{counters[key] - 1}")
    coverage = coverage_report(result, points, issues, inactive,
                               exceptions or [])
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
