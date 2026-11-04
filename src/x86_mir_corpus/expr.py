"""Expression AST, parser, and the small evaluator the corpus reader needs.

The evaluator understands only the constructs the legalizer builder chains and
their guards use.  It deliberately does not try to be a C++ interpreter: an
unknown construct raises and the caller records it.
"""

import contextlib
from dataclasses import dataclass, field
from typing import Any

from .lexer import Token, matching


class ExprError(Exception):
    pass


@dataclass(frozen=True)
class Opcode:
    name: str

    def __str__(self):
        return self.name


@dataclass
class Num:
    value: int


@dataclass
class Str:
    value: str


@dataclass
class Ident:
    name: str


@dataclass
class Call:
    name: str
    args: list


@dataclass
class Unary:
    op: str
    x: Any


@dataclass
class Binary:
    op: str
    left: Any
    right: Any


@dataclass
class Ternary:
    cond: Any
    then: Any
    other: Any


@dataclass
class ListLit:
    items: list


@dataclass
class Lambda:
    text: str
    line: int
    params: str
    body: list = field(default_factory=list)


@dataclass
class Unknown:
    text: str


# --- LLT helpers -----------------------------------------------------------
#
# The tuples mirror LLT's kinds, not just its printed spelling:
#
#   ("p", as)             pointer (size is target data layout, not here)
#   ("s", bits)           Kind::ANY_SCALAR  (LLT::scalar)
#   ("i", bits)           Kind::INTEGER     (LLT::integer, extended LLT)
#   ("f", bits, semantics) Kind::FLOAT      (IEEE / bfloat / x87 / ppc)
#   ("v", lanes, elem)    fixed vector
#   ("nxv", lanes, elem)  scalable vector
#
# ``LLT::scalar(32)`` and ``LLT::integer(32)`` compare unequal when a target
# enables extended LLT (AArch64 does: LLT::setUseExtended(true)), so the two
# are separate kinds here as well.

LLT_KINDS = ("p", "s", "i", "f", "v", "nxv")

def ptr(as_=0):
    return ("p", int(as_))


def scalar(bits):
    return ("s", int(bits))


def integer(bits):
    return ("i", int(bits))


def float_(bits, semantics="ieee"):
    return ("f", int(bits), semantics)


def vec(lanes, elem):
    if isinstance(elem, int):
        elem = scalar(elem)
    return ("v", int(lanes), elem)


def scalable_vec(lanes, elem):
    if isinstance(elem, int):
        elem = scalar(elem)
    return ("nxv", int(lanes), elem)


def element_count(kind, lanes):
    return ("ec", kind, int(lanes))


def is_llt(v):
    if not isinstance(v, tuple) or len(v) < 2 or v[0] not in LLT_KINDS:
        return False
    if v[0] in ("v", "nxv"):
        return len(v) == 3 and is_llt(v[2])
    if v[0] == "f":
        return len(v) == 3
    return len(v) == 2


def is_ptr(t):
    return t[0] == "p"


def is_vec(t):
    return t[0] in ("v", "nxv")


def is_scalable(t):
    return t[0] == "nxv"


def is_float(t):
    return t[0] == "f"


def is_int(t):
    """Integer-like scalar or vector element (ANY_SCALAR or INTEGER)."""
    return t[0] in ("s", "i")


def is_scalar(t):
    """LLT::isScalar(): any-scalar, integer, or float -- never a pointer."""
    return t[0] in ("s", "i", "f")


def elem_type(t):
    return t[2] if is_vec(t) else t


def llt_name(t):
    kind = t[0]
    if kind == "p":
        return f"p{t[1]}"
    if kind == "s":
        return f"s{t[1]}"
    if kind == "i":
        return f"i{t[1]}"
    if kind == "f":
        bits, sem = t[1], t[2]
        if sem == "bfloat":
            return "bf16"
        if sem == "x86":
            return f"f{bits}"
        if sem == "ppc":
            return "ppcf128"
        return f"f{bits}"
    if kind in ("v", "nxv"):
        return f"{kind}{t[1]}{llt_name(t[2])}"
    raise ExprError(f"bad LLT {t!r}")


def llt_bits(t, ptr_bits=0):
    """LLT::getSizeInBits() as a plain integer (known-minimum for scalable)."""
    if t[0] == "p":
        return ptr_bits
    if t[0] in ("s", "i", "f"):
        return t[1]
    if t[0] in ("v", "nxv"):
        return t[1] * llt_bits(t[2], ptr_bits)
    raise ExprError(f"bad LLT {t!r}")


def llt_lanes(t):
    return t[1] if is_vec(t) else 1


def llt_scalar_bits(t, ptr_bits=0):
    """LLT::getScalarSizeInBits(); pointers resolve through the data layout."""
    if t[0] == "p":
        return ptr_bits
    if is_vec(t):
        return llt_scalar_bits(t[2], ptr_bits)
    return t[1]


def llt_kind(t):
    return t[0]


def llt_is_fixed_vector(t):
    return t[0] == "v"


# --- parser ----------------------------------------------------------------

_BIN_PREC = {
    "||": 1,
    "&&": 2,
    "|": 3,
    "^": 4,
    "&": 5,
    "==": 6, "!=": 6,
    "<": 7, ">": 7, "<=": 7, ">=": 7,
    "<<": 8, ">>": 8,
    "+": 9, "-": 9,
    "*": 10, "/": 10, "%": 10,
}


class Parser:
    def __init__(self, toks, index=0, end=None):
        self.toks = toks
        self.i = index
        self.end = len(toks) if end is None else end

    def peek(self, offset=0):
        j = self.i + offset
        if j >= self.end:
            return Token("eof", "", toks_line(self.toks, self.end), 0)
        return self.toks[j]

    def take(self):
        t = self.peek()
        self.i += 1
        return t

    def expect(self, text):
        t = self.take()
        if t.text != text:
            raise ExprError(f"expected {text!r}, got {t.text!r} at line {t.line}")
        return t

    def at(self, text):
        return self.peek().text == text

    def parse_expr(self):
        return self.parse_ternary()

    def parse_ternary(self):
        cond = self.parse_binary(1)
        if self.at("?"):
            self.take()
            then = self.parse_expr()
            self.expect(":")
            other = self.parse_expr()
            return Ternary(cond, then, other)
        return cond

    def parse_binary(self, min_prec):
        left = self.parse_unary()
        while True:
            t = self.peek()
            prec = _BIN_PREC.get(t.text)
            if prec is None or prec < min_prec:
                break
            self.take()
            right = self.parse_binary(prec + 1)
            left = Binary(t.text, left, right)
        return left

    def parse_unary(self):
        t = self.peek()
        if t.text in ("!", "-", "+", "~", "*", "&"):
            self.take()
            return Unary(t.text, self.parse_unary())
        return self.parse_postfix()

    def parse_postfix(self):
        node = self.parse_primary()
        while True:
            t = self.peek()
            if t.text == "::" and isinstance(node, Ident):
                self.take()
                name = self.take()
                node = Ident(f"{node.name}::{name.text}")
            elif t.text in (".", "->"):
                self.take()
                name = self.take()
                if self.at("("):
                    args, _ = self.parse_args()
                    node = Call(f"{node_name(node)}.{name.text}", args)
                else:
                    node = Ident(f"{node_name(node)}.{name.text}")
            elif t.text == "(":
                args, _ = self.parse_args()
                if isinstance(node, Call):
                    # C++ predicate objects: typeIs(0, s32)(Query).  The
                    # trailing call applies the predicate; its arguments are
                    # appended so the predicate's own arguments survive.
                    node = Call(node.name, list(node.args) + list(args))
                else:
                    node = Call(node_name(node), args)
            elif t.text == "[":
                self.take()
                idx = self.parse_expr()
                self.expect("]")
                node = Call(f"{node_name(node)}.__index__", [idx])
            else:
                break
        return node

    def parse_primary(self):
        t = self.peek()
        if t.text == "(":
            self.take()
            node = self.parse_expr()
            self.expect(")")
            return node
        if t.text == "{":
            return self.parse_list()
        if t.text == "[":
            return self.parse_lambda()
        if t.kind == "num":
            self.take()
            return Num(parse_int(t.text))
        if t.kind in ("str", "char"):
            self.take()
            return Str(t.text)
        if t.kind == "id":
            self.take()
            if t.text == "true":
                return Num(1)
            if t.text == "false":
                return Num(0)
            return Ident(t.text)
        raise ExprError(f"unexpected {t.text!r} at line {t.line}")

    def parse_list(self):
        self.expect("{")
        items = []
        if self.at("}"):
            self.take()
            return ListLit(items)
        while True:
            items.append(self.parse_expr())
            if self.at(","):
                self.take()
                continue
            self.expect("}")
            return ListLit(items)

    def parse_lambda(self):
        start = self.i
        open_line = self.peek().line
        close = matching(self.toks, start, self.end)
        if close < 0:
            raise ExprError(f"unterminated lambda at line {open_line}")
        self.i = close + 1
        params = ""
        if self.at("("):
            p_open = self.i
            p_close = matching(self.toks, p_open, self.end)
            if p_close < 0:
                raise ExprError(f"unterminated lambda parameters at line {open_line}")
            params = " ".join(t.text for t in self.toks[p_open + 1:p_close])
            self.i = p_close + 1
        body = []
        if self.at("{"):
            b_open = self.i
            b_close = matching(self.toks, b_open, self.end)
            if b_close < 0:
                raise ExprError(f"unterminated lambda body at line {open_line}")
            body = self.toks[b_open + 1:b_close]
            self.i = b_close + 1
        text = " ".join(t.text for t in self.toks[start:self.i])
        return Lambda(text, open_line, params, body)

    def parse_args(self):
        self.expect("(")
        args = []
        if self.at(")"):
            self.take()
            return args, ""
        while True:
            try:
                args.append(self.parse_expr())
            except ExprError:
                # Consume the raw argument so the builder chain can continue.
                start = self.i
                depth = 0
                while self.i < self.end:
                    t = self.peek().text
                    if t in ("(", "[", "{"):
                        depth += 1
                    elif t in (")", "]", "}"):
                        if depth == 0:
                            break
                        depth -= 1
                    elif t == "," and depth == 0:
                        break
                    self.i += 1
                args.append(Unknown(" ".join(x.text for x in self.toks[start:self.i])))
            if self.at(","):
                self.take()
                continue
            self.expect(")")
            return args, ""


def toks_line(toks, i):
    if not toks:
        return 0
    return toks[min(i, len(toks) - 1)].line


def node_name(node):
    if isinstance(node, Ident):
        return node.name
    if isinstance(node, Call):
        return node.name
    return "?"


def parse_int(text):
    s = text.rstrip("uUlL")
    if s.startswith(("0x", "0X")):
        return int(s, 16)
    if s.startswith(("0b", "0B")):
        return int(s, 2)
    return int(s, 10)


# --- evaluator -------------------------------------------------------------

class EvalError(ExprError):
    pass


def eval_expr(node, env):
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Str):
        return node.value
    if isinstance(node, Ident):
        if node.name in env:
            return env[node.name]
        if node.name.startswith("G_") and node.name.upper() == node.name:
            return Opcode(node.name)
        raise EvalError(f"unknown identifier {node.name}")
    if isinstance(node, Unary):
        v = eval_expr(node.x, env)
        if node.op == "!":
            return not truthy(v)
        if node.op == "-":
            return -v
        if node.op == "+":
            return +v
        if node.op == "~":
            return ~v
        raise EvalError(f"unsupported unary {node.op}")
    if isinstance(node, Binary):
        if node.op == "&&":
            lhs = eval_expr(node.left, env)
            return truthy(lhs) and truthy(eval_expr(node.right, env))
        if node.op == "||":
            lhs = eval_expr(node.left, env)
            return truthy(lhs) or truthy(eval_expr(node.right, env))
        lhs = eval_expr(node.left, env)
        rhs = eval_expr(node.right, env)
        return apply_binary(node.op, lhs, rhs)
    if isinstance(node, Ternary):
        return eval_expr(node.then if truthy(eval_expr(node.cond, env)) else node.other, env)
    if isinstance(node, ListLit):
        return [eval_expr(x, env) for x in node.items]
    if isinstance(node, Lambda):
        return node
    if isinstance(node, Call):
        return eval_call(node, env)
    if isinstance(node, Unknown):
        raise EvalError(f"unknown construct {node.text!r}")
    raise EvalError(f"cannot evaluate {node!r}")


def apply_binary(op, lhs, rhs):
    if op == "+":
        return lhs + rhs
    if op == "-":
        return lhs - rhs
    if op == "*":
        return lhs * rhs
    if op == "/":
        return lhs // rhs
    if op == "%":
        return lhs % rhs
    if op == "<<":
        return lhs << rhs
    if op == ">>":
        return lhs >> rhs
    if op == "&":
        return lhs & rhs
    if op == "|":
        return lhs | rhs
    if op == "^":
        return lhs ^ rhs
    if op == "==":
        return lhs == rhs
    if op == "!=":
        return lhs != rhs
    if op == "<":
        return lhs < rhs
    if op == ">":
        return lhs > rhs
    if op == "<=":
        return lhs <= rhs
    if op == ">=":
        return lhs >= rhs
    raise EvalError(f"unsupported operator {op}")


def _llt_extended(env):
    return bool(env.get("__llt_extended__", True))


def _llt_vector(name, args):
    lanes = int(args[0])
    elem = args[1]
    if not is_llt(elem):
        elem = scalar(int(elem))
    return scalable_vec(lanes, elem) if name == "LLT::scalable_vector" else vec(lanes, elem)


def _float_like(bits, semantics, env):
    """LLT float constructors honour LLT::getUseExtended() except bfloat/ppc/x87."""
    if not _llt_extended(env) and semantics == "ieee":
        return scalar(bits)
    return float_(bits, semantics)


def eval_call(node, env):
    name = node.name
    if name in ("LLT::scalar", "LLT::integer", "LLT::pointer",
                "LLT::fixed_vector", "LLT::scalable_vector", "LLT::vector",
                "LLT::scalarOrVector"):
        args = [eval_expr(a, env) for a in node.args]
        if name == "LLT::scalar":
            return scalar(int(args[0]))
        if name == "LLT::integer":
            return integer(int(args[0])) if _llt_extended(env) else scalar(int(args[0]))
        if name == "LLT::pointer":
            return ptr(int(args[0]))
        if name == "LLT::scalarOrVector":
            count, elem = args[0], args[1]
            if isinstance(count, tuple) and len(count) == 3 and count[0] == "ec":
                if count[1] == "fixed" and count[2] == 1:
                    return elem if is_llt(elem) else scalar(int(elem))
                if not is_llt(elem):
                    elem = scalar(int(elem))
                factory = scalable_vec if count[1] == "scalable" else vec
                return factory(count[2], elem)
            raise EvalError("scalarOrVector needs an ElementCount")
        if name == "LLT::vector":
            count, elem = args[0], args[1]
            if isinstance(count, tuple) and len(count) == 3 and count[0] == "ec":
                if not is_llt(elem):
                    elem = scalar(int(elem))
                factory = scalable_vec if count[1] == "scalable" else vec
                return factory(count[2], elem)
            return _llt_vector(name, args)
        return _llt_vector(name, args)
    if name in ("ElementCount::getFixed", "ElementCount::getScalable"):
        args = [eval_expr(a, env) for a in node.args]
        return element_count(name.rsplit("::", 1)[1].replace("get", "").lower(),
                             int(args[0]))
    if name in ("LLT::float16", "LLT::float32", "LLT::float64", "LLT::float128",
                "LLT::bfloat16", "LLT::x86fp80", "LLT::ppcf128", "LLT::floatIEEE"):
        bits = {"LLT::float16": 16, "LLT::float32": 32, "LLT::float64": 64,
                "LLT::float128": 128, "LLT::bfloat16": 16, "LLT::x86fp80": 80,
                "LLT::ppcf128": 128}[name]
        if name == "LLT::floatIEEE":
            bits = int(eval_expr(node.args[0], env))
        elif name in ("LLT::bfloat16",):
            return float_(16, "bfloat")
        elif name == "LLT::x86fp80":
            return float_(80, "x86")
        elif name == "LLT::ppcf128":
            return float_(128, "ppc")
        return _float_like(bits, "ieee", env)
    if name == "LLT::token":
        return scalar(0)
    if name.endswith(".getTargetLowering"):
        return "TargetLowering"
    if name.endswith(".getTargetMachine"):
        return "TargetMachine"
    if name.startswith(("Subtarget.", "ST.")):
        method = name.split(".", 1)[1]
        feats = env.get("__features__", {})
        if method in feats:
            return feats[method]
        raise EvalError(f"unknown subtarget predicate {method}")
    if name.endswith(".getCodeModel"):
        value = env.get("__code_model__")
        if value is None:
            raise EvalError("getCodeModel without a target code model")
        return value
    if name in ("Query.Types.__index__",):
        raise EvalError("Query.Types.__index__ is not a value")
    if name.endswith(".getNumElements"):
        obj = env.get(name[:-len(".getNumElements")])
        if not is_llt(obj):
            raise EvalError(f"getNumElements on non-LLT {name}")
        return llt_lanes(obj)
    if name.endswith(".getScalarSizeInBits"):
        obj = env.get(name[:-len(".getScalarSizeInBits")])
        if not is_llt(obj):
            raise EvalError(f"getScalarSizeInBits on non-LLT {name}")
        return llt_scalar_bits(obj, env.get("__pointer_bits__", 0))
    if name.endswith(".getSizeInBits"):
        obj = env.get(name[:-len(".getSizeInBits")])
        if not is_llt(obj):
            raise EvalError(f"getSizeInBits on non-LLT {name}")
        return llt_bits(obj, env.get("__pointer_bits__", 0))
    if name.endswith(".getSizeInBytes"):
        obj = env.get(name[:-len(".getSizeInBytes")])
        if not is_llt(obj):
            raise EvalError(f"getSizeInBytes on non-LLT {name}")
        return llt_bits(obj, env.get("__pointer_bits__", 0)) // 8
    if name.endswith(".getElementType"):
        obj = env.get(name[:-len(".getElementType")])
        if not is_llt(obj):
            raise EvalError(f"getElementType on non-LLT {name}")
        return elem_type(obj)
    if name.endswith(".getElementCount"):
        obj = env.get(name[:-len(".getElementCount")])
        if not is_llt(obj):
            raise EvalError(f"getElementCount on non-LLT {name}")
        return element_count("scalable" if is_scalable(obj) else "fixed",
                             llt_lanes(obj))
    if name.endswith(".isScalar"):
        obj = env.get(name[:-len(".isScalar")])
        return is_llt(obj) and is_scalar(obj)
    if name.endswith(".isVector"):
        obj = env.get(name[:-len(".isVector")])
        return is_llt(obj) and is_vec(obj)
    if name.endswith(".isFixedVector"):
        obj = env.get(name[:-len(".isFixedVector")])
        return is_llt(obj) and llt_is_fixed_vector(obj)
    if name.endswith(".isScalableVector"):
        obj = env.get(name[:-len(".isScalableVector")])
        return is_llt(obj) and is_scalable(obj)
    if name.endswith(".isPointer"):
        obj = env.get(name[:-len(".isPointer")])
        return is_llt(obj) and is_ptr(obj)
    # A literal predicate call such as typeIs(0, s8) is not a value.
    raise EvalError(f"unsupported call {name}")


def call_names(node):
    """Every bare call name in *node* (lambda bodies stay opaque)."""
    out = set()

    def walk(value):
        if isinstance(value, Call):
            out.add(value.name)
            for arg in value.args:
                walk(arg)
        elif isinstance(value, ListLit):
            for item in value.items:
                walk(item)
        elif isinstance(value, Unary):
            walk(value.x)
        elif isinstance(value, Binary):
            walk(value.left)
            walk(value.right)
        elif isinstance(value, Ternary):
            walk(value.cond)
            walk(value.then)
            walk(value.other)

    walk(node)
    return out


def truthy(v):
    if isinstance(v, Opcode):
        return True
    return bool(v)


# --- collection ------------------------------------------------------------

def collect_types(node, env, out):
    """Add every LLT mentioned anywhere in node, including untaken branches."""
    if isinstance(node, Ident):
        v = env.get(node.name)
        if is_llt(v):
            out.add(v)
    elif isinstance(node, Call):
        if node.name.startswith("LLT::"):
            try:
                v = eval_expr(node, env)
                if is_llt(v):
                    out.add(v)
            except EvalError:
                pass
        for a in node.args:
            collect_types(a, env, out)
    elif isinstance(node, Lambda):
        collect_lambda_types(node, env, out)
    elif isinstance(node, (Unary,)):
        collect_types(node.x, env, out)
    elif isinstance(node, Binary):
        collect_types(node.left, env, out)
        collect_types(node.right, env, out)
    elif isinstance(node, Ternary):
        collect_types(node.cond, env, out)
        collect_types(node.then, env, out)
        collect_types(node.other, env, out)
    elif isinstance(node, ListLit):
        for x in node.items:
            collect_types(x, env, out)


def collect_lambda_types(lam, env, out):
    body = lam.body
    i = 0
    while i < len(body):
        if body[i].text == "LLT" and i + 3 < len(body) and body[i + 1].text == "::" \
                and body[i + 2].kind == "id" and body[i + 3].text == "(":
            close = matching(body, i + 3)
            if close > 0:
                try:
                    node = Parser(body, i, close + 1).parse_expr()
                    collect_types(node, env, out)
                except ExprError:
                    pass
                i = close + 1
                continue
        if body[i].kind == "id" and is_llt(env.get(body[i].text)):
            out.add(env[body[i].text])
        i += 1


_PRED_SLOT1 = {
    "typeIs", "typeIsNot", "typeInSet", "scalarOrEltWiderThan",
    "scalarNarrowerThan", "scalarOrEltNarrowerThan", "scalarWiderThan",
    "sizeNotPow2", "isScalar", "isPointer", "isVector",
}
_PRED_SLOT2 = {"typePairInSet", "typePairIs"}


def collect_slots(node, env, out):
    if isinstance(node, Call):
        if node.name in _PRED_SLOT1 and node.args:
            with contextlib.suppress(EvalError, TypeError, ValueError):
                out.add(int(eval_expr(node.args[0], env)))
        if node.name in _PRED_SLOT2 and len(node.args) >= 2:
            for a in node.args[:2]:
                with contextlib.suppress(EvalError, TypeError, ValueError):
                    out.add(int(eval_expr(a, env)))
        for a in node.args:
            collect_slots(a, env, out)
    elif isinstance(node, ListLit):
        for x in node.items:
            collect_slots(x, env, out)
    elif isinstance(node, Unary):
        collect_slots(node.x, env, out)
    elif isinstance(node, Binary):
        collect_slots(node.left, env, out)
        collect_slots(node.right, env, out)
    elif isinstance(node, Ternary):
        collect_slots(node.cond, env, out)
        collect_slots(node.then, env, out)
        collect_slots(node.other, env, out)
    elif isinstance(node, Lambda):
        body = node.body
        i = 0
        while i < len(body):
            if body[i].text == "Types" and i + 1 < len(body) and body[i + 1].text == "[":
                j = i + 2
                if body[j].kind == "num":
                    with contextlib.suppress(ValueError):
                        out.add(int(body[j].text))
                elif body[j].kind == "id" and isinstance(env.get(body[j].text), int):
                    out.add(env[body[j].text])
                i = j
            i += 1


def lambda_has_mem(lam):
    return any(t.text == "MMODescrs" for t in lam.body)


def lambda_has(lam, token_text):
    return any(t.text == token_text for t in lam.body)
