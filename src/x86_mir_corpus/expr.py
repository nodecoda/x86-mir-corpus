"""Expression AST, parser, and the small evaluator the corpus reader needs.

The evaluator understands only the constructs the legalizer builder chains and
their guards use.  It deliberately does not try to be a C++ interpreter: an
unknown construct raises and the caller records it.
"""

import contextlib
from dataclasses import dataclass, field
from typing import Any

from . import dsl as _dsl
from .lexer import Token, lex, matching


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
    # ``a.b()`` keeps ``a`` so chained queries such as
    # ``Query.Types[0].getScalarType().isFloat16()`` stay evaluable.
    receiver: Any = None


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
                    node = Call(f"{node_name(node)}.{name.text}", args,
                                receiver=node)
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


_TABLE = _dsl.load()
_LLT_QUERIES = {entry["name"]: entry for entry in _TABLE["llt_queries"]}
_ELEMENT_COUNT_OPS = {
    entry["name"]: entry for entry in _TABLE["element_count"]}
_CODE_MODEL_VALUES = _dsl.enum_values("CodeModel", _TABLE)

# Registries are the runtime half of the v2 symbol table: every entry the
# table marks ``exact`` must have an implementation here, and vice versa.
# ``tests/test_dsl_dispatch.py`` enforces that equality.
LLT_CTOR_IMPL = {}
LLT_QUERY_IMPL = {}
ELEMENT_COUNT_IMPL = {}


def llt_ctor(name):
    def register(fn):
        LLT_CTOR_IMPL[name] = fn
        return fn
    return register


def llt_query(name):
    def register(fn):
        LLT_QUERY_IMPL[name] = fn
        return fn
    return register


def element_count_op(name):
    def register(fn):
        ELEMENT_COUNT_IMPL[name] = fn
        return fn
    return register


def _int(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    raise EvalError(f"expected an integer, got {value!r}")


def _is_ec(value):
    return (isinstance(value, tuple) and len(value) == 3
            and value[0] == "ec")


def _ec_count(value):
    return value[2]


def _ec_is_scalable(value):
    return value[1] == "scalable"


def _round_up_bytes(bits):
    return (int(bits) + 7) // 8


def _llt_element_size(t):
    return t[1] if not is_vec(t) else _llt_element_size(t[2])


def _float_semantics_bits(name):
    return {
        "S_IEEEhalf": 16,
        "S_IEEEsingle": 32,
        "S_IEEEdouble": 64,
        "S_IEEEquad": 128,
        "S_BFloat": 16,
        "S_x87DoubleExtended": 80,
        "S_PPCDoubleDouble": 128,
    }.get(str(name).rsplit("::", 1)[-1])


# --- LLT constructors ------------------------------------------------------

@llt_ctor("scalar")
def _ctor_scalar(args, env):
    return scalar(_int(eval_expr(args[0], env)))


@llt_ctor("integer")
def _ctor_integer(args, env):
    bits = _int(eval_expr(args[0], env))
    return integer(bits) if _llt_extended(env) else scalar(bits)


@llt_ctor("pointer")
def _ctor_pointer(args, env):
    return ptr(_int(eval_expr(args[0], env)))


@llt_ctor("token")
def _ctor_token(args, env):
    return scalar(0)


@llt_ctor("fixed_vector")
def _ctor_fixed_vector(args, env):
    return vec(_int(eval_expr(args[0], env)),
               eval_expr(args[1], env))


@llt_ctor("scalable_vector")
def _ctor_scalable_vector(args, env):
    return scalable_vec(_int(eval_expr(args[0], env)),
                        eval_expr(args[1], env))


@llt_ctor("vector")
def _ctor_vector(args, env):
    count, elem = eval_expr(args[0], env), eval_expr(args[1], env)
    if _is_ec(count):
        if not is_llt(elem):
            elem = scalar(_int(elem))
        factory = scalable_vec if _ec_is_scalable(count) else vec
        return factory(_ec_count(count), elem)
    return vec(_int(count), elem)


@llt_ctor("scalarOrVector")
def _ctor_scalar_or_vector(args, env):
    count, elem = eval_expr(args[0], env), eval_expr(args[1], env)
    if not _is_ec(count):
        raise EvalError("scalarOrVector needs an ElementCount")
    if not is_llt(elem):
        elem = scalar(_int(elem))
    if not _ec_is_scalable(count) and _ec_count(count) == 1:
        return elem
    factory = scalable_vec if _ec_is_scalable(count) else vec
    return factory(_ec_count(count), elem)


@llt_ctor("floatingPoint")
def _ctor_floating_point(args, env):
    value = eval_expr(args[0], env)
    bits = _float_semantics_bits(value)
    if bits is None:
        raise EvalError(f"unknown floatingPoint semantics {value}")
    semantics = {16: "bfloat" if "BFloat" in str(value) else "ieee",
                 80: "x86", 128: "ppc" if "PPC" in str(value) else "ieee"}
    return _float_like(bits, semantics.get(bits, "ieee"), env)


@llt_ctor("floatIEEE")
def _ctor_float_ieee(args, env):
    return _float_like(_int(eval_expr(args[0], env)), "ieee", env)


def _float_ctor(bits, semantics):
    def ctor(args, env):
        return _float_like(bits, semantics, env)
    return ctor


for _name, _bits, _sem in (
        ("float16", 16, "ieee"), ("float32", 32, "ieee"),
        ("float64", 64, "ieee"), ("float128", 128, "ieee"),
        ("bfloat16", 16, "bfloat"), ("x86fp80", 80, "x86"),
        ("ppcf128", 128, "ppc")):
    LLT_CTOR_IMPL[_name] = _float_ctor(_bits, _sem)


# --- LLT queries -----------------------------------------------------------

@llt_query("isValid")
def _q_is_valid(t, args, env):
    return is_llt(t)


@llt_query("isToken")
def _q_is_token(t, args, env):
    return t == ("s", 0)


@llt_query("isAnyScalar")
def _q_is_any_scalar(t, args, env):
    return t[0] == "s"


@llt_query("isInteger")
def _q_is_integer(t, args, env):
    return t[0] == "i"


@llt_query("isFloat")
def _q_is_float(t, args, env):
    return t[0] == "f"


@llt_query("isPointer")
def _q_is_pointer(t, args, env):
    return t[0] == "p"


@llt_query("isAnyVector")
def _q_is_any_vector(t, args, env):
    return is_vec(t) and t[2][0] == "s"


@llt_query("isIntegerVector")
def _q_is_integer_vector(t, args, env):
    return is_vec(t) and t[2][0] == "i"


@llt_query("isFloatVector")
def _q_is_float_vector(t, args, env):
    return is_vec(t) and t[2][0] == "f"


@llt_query("isPointerVector")
def _q_is_pointer_vector(t, args, env):
    return is_vec(t) and t[2][0] == "p"


@llt_query("isPointerOrPointerVector")
def _q_is_pointer_or_pointer_vector(t, args, env):
    return is_ptr(t) or (is_vec(t) and t[2][0] == "p")


@llt_query("isFloatOrFloatVector")
def _q_is_float_or_float_vector(t, args, env):
    return is_float(t) or (is_vec(t) and t[2][0] == "f")


@llt_query("isScalar")
def _q_is_scalar(t, args, env):
    return is_scalar(t)


@llt_query("isVector")
def _q_is_vector(t, args, env):
    return is_vec(t)


@llt_query("isFloatIEEE")
def _q_is_float_ieee(t, args, env):
    return is_float(t) and t[2] == "ieee"


@llt_query("isFloat16")
def _q_is_float16(t, args, env):
    if not _llt_extended(env):
        return t[0] == "s" and t[1] == 16
    return is_float(t) and t[1] == 16 and t[2] == "ieee"


@llt_query("isFloat32")
def _q_is_float32(t, args, env):
    return is_float(t) and t[1] == 32 and t[2] == "ieee"


@llt_query("isFloat64")
def _q_is_float64(t, args, env):
    return is_float(t) and t[1] == 64 and t[2] == "ieee"


@llt_query("isFloat128")
def _q_is_float128(t, args, env):
    return is_float(t) and t[1] == 128 and t[2] == "ieee"


@llt_query("isBFloat16")
def _q_is_bfloat16(t, args, env):
    return is_float(t) and t[1] == 16 and t[2] == "bfloat"


@llt_query("isX86FP80")
def _q_is_x86fp80(t, args, env):
    return is_float(t) and t[1] == 80 and t[2] == "x86"


@llt_query("isPPCF128")
def _q_is_ppcf128(t, args, env):
    return is_float(t) and t[1] == 128 and t[2] == "ppc"


@llt_query("getNumElements")
def _q_get_num_elements(t, args, env):
    return llt_lanes(t)


@llt_query("getElementCount")
def _q_get_element_count(t, args, env):
    if not is_vec(t):
        raise EvalError("getElementCount on a non-vector LLT")
    return element_count("scalable" if is_scalable(t) else "fixed", t[1])


@llt_query("isScalable")
def _q_is_scalable(t, args, env):
    return is_scalable(t)


@llt_query("isFixedVector")
def _q_is_fixed_vector(t, args, env):
    return t[0] == "v"


@llt_query("isScalableVector")
def _q_is_scalable_vector(t, args, env):
    return t[0] == "nxv"


@llt_query("getSizeInBits")
def _q_get_size_in_bits(t, args, env):
    return llt_bits(t, env.get("__pointer_bits__", 0))


@llt_query("getSizeInBytes")
def _q_get_size_in_bytes(t, args, env):
    return _round_up_bytes(llt_bits(t, env.get("__pointer_bits__", 0)))


@llt_query("getScalarType")
def _q_get_scalar_type(t, args, env):
    return elem_type(t) if is_vec(t) else t


@llt_query("getElementType")
def _q_get_element_type(t, args, env):
    if not is_vec(t):
        raise EvalError("getElementType on a non-vector LLT")
    return t[2]


@llt_query("getScalarSizeInBits")
def _q_get_scalar_size(t, args, env):
    return llt_scalar_bits(t, env.get("__pointer_bits__", 0))


@llt_query("getAddressSpace")
def _q_get_address_space(t, args, env):
    target = elem_type(t) if is_vec(t) else t
    if target[0] != "p":
        raise EvalError("getAddressSpace on a non-pointer LLT")
    return target[1]


@llt_query("changeVectorElementType")
def _q_change_vector_element_type(t, args, env):
    new_elem = eval_expr(args[0], env)
    if not is_vec(t):
        raise EvalError("changeVectorElementType on a non-vector LLT")
    factory = scalable_vec if is_scalable(t) else vec
    return factory(t[1], new_elem)


@llt_query("changeElementType")
def _q_change_element_type(t, args, env):
    new_elem = eval_expr(args[0], env)
    if not is_vec(t):
        return new_elem
    factory = scalable_vec if is_scalable(t) else vec
    return factory(t[1], new_elem)


@llt_query("changeElementSize")
def _q_change_element_size(t, args, env):
    size = _int(eval_expr(args[0], env))
    if is_ptr(t):
        raise EvalError("changeElementSize on a pointer LLT")
    if is_vec(t):
        new_elem = _change_element_size(elem_type(t), size, env)
        factory = scalable_vec if is_scalable(t) else vec
        return factory(t[1], new_elem)
    return _change_element_size(t, size, env)


def _change_element_size(t, size, env):
    if t[0] == "i":
        return integer(size)
    if t[0] == "f" and t[2] == "ieee":
        return float_(size, "ieee")
    return scalar(size)


@llt_query("changeVectorElementCount")
def _q_change_vector_element_count(t, args, env):
    count = eval_expr(args[0], env)
    if not is_vec(t):
        raise EvalError("changeVectorElementCount on a non-vector LLT")
    if not _is_ec(count):
        raise EvalError("changeVectorElementCount needs an ElementCount")
    factory = scalable_vec if _ec_is_scalable(count) else vec
    return factory(_ec_count(count), t[2])


@llt_query("changeElementCount")
def _q_change_element_count(t, args, env):
    count = eval_expr(args[0], env)
    if not _is_ec(count):
        raise EvalError("changeElementCount needs an ElementCount")
    elem = elem_type(t) if is_vec(t) else t
    if not _ec_is_scalable(count) and _ec_count(count) == 1:
        return elem
    factory = scalable_vec if _ec_is_scalable(count) else vec
    return factory(_ec_count(count), elem)


@llt_query("divide")
def _q_divide(t, args, env):
    factor = _int(eval_expr(args[0], env))
    if factor == 1:
        raise EvalError("divide by one is not a legal LLT mutation")
    if is_vec(t):
        if t[1] % factor:
            raise EvalError("divide: lane count is not divisible")
        factory = scalable_vec if is_scalable(t) else vec
        return factory(t[1] // factor, t[2])
    bits = llt_scalar_bits(t, env.get("__pointer_bits__", 0))
    if bits % factor:
        raise EvalError("divide: scalar size is not divisible")
    return integer(bits // factor) if t[0] == "i" else scalar(bits // factor)


@llt_query("multiplyElements")
def _q_multiply_elements(t, args, env):
    factor = _int(eval_expr(args[0], env))
    if is_vec(t):
        factory = scalable_vec if is_scalable(t) else vec
        return factory(t[1] * factor, t[2])
    return vec(factor, t)


@llt_query("isByteSized")
def _q_is_byte_sized(t, args, env):
    return llt_bits(t, env.get("__pointer_bits__", 0)) % 8 == 0


@llt_query("changeToInteger")
def _q_change_to_integer(t, args, env):
    if is_ptr(t):
        return t
    if is_vec(t):
        factory = scalable_vec if is_scalable(t) else vec
        return factory(t[1], integer(llt_scalar_bits(t, env.get("__pointer_bits__", 0))))
    return integer(llt_bits(t, env.get("__pointer_bits__", 0)))


@llt_query("getUseExtended")
def _q_get_use_extended(t, args, env):
    return _llt_extended(env)


@llt_query("setUseExtended")
def _q_set_use_extended(t, args, env):
    return None


# --- ElementCount ----------------------------------------------------------

@element_count_op("getFixed")
def _ec_get_fixed(value, args, env):
    return element_count("fixed", _int(eval_expr(args[0], env)))


@element_count_op("getScalable")
def _ec_get_scalable(value, args, env):
    return element_count("scalable", _int(eval_expr(args[0], env)))


@element_count_op("get")
def _ec_get(value, args, env):
    count = _int(eval_expr(args[0], env))
    scalable = bool(eval_expr(args[1], env))
    return element_count("scalable" if scalable else "fixed", count)


@element_count_op("isScalar")
def _ec_is_scalar(value, args, env):
    return not _ec_is_scalable(value) and _ec_count(value) == 1


@element_count_op("isVector")
def _ec_is_vector(value, args, env):
    return ((_ec_is_scalable(value) and _ec_count(value) != 0)
            or _ec_count(value) > 1)


@element_count_op("isPowerOf2")
def _ec_is_pow2(value, args, env):
    count = _ec_count(value)
    return count > 0 and (count & (count - 1)) == 0


@element_count_op("isZero")
def _ec_is_zero(value, args, env):
    return _ec_count(value) == 0


@element_count_op("isNonZero")
def _ec_is_nonzero(value, args, env):
    return _ec_count(value) != 0


@element_count_op("getKnownMinValue")
def _ec_get_known_min_value(value, args, env):
    return _ec_count(value)


@element_count_op("isScalable")
def _ec_is_scalable_op(value, args, env):
    return _ec_is_scalable(value)


@element_count_op("isFixed")
def _ec_is_fixed(value, args, env):
    return not _ec_is_scalable(value)


@element_count_op("isKnownEven")
def _ec_is_known_even(value, args, env):
    return _ec_count(value) % 2 == 0


@element_count_op("isKnownMultipleOf")
def _ec_is_known_multiple_of(value, args, env):
    return _ec_count(value) % _int(eval_expr(args[0], env)) == 0


@element_count_op("divideCoefficientBy")
def _ec_divide_coefficient(value, args, env):
    return element_count(value[1], _ec_count(value)
                         // _int(eval_expr(args[0], env)))


@element_count_op("coefficientNextPowerOf2")
def _ec_coefficient_next_pow2(value, args, env):
    count = max(1, _ec_count(value))
    return element_count(value[1], 1 << (count - 1).bit_length())


@element_count_op("hasKnownScalarFactor")
def _ec_has_known_scalar_factor(value, args, env):
    factor = eval_expr(args[0], env)
    if _is_ec(factor):
        factor = _ec_count(factor)
    return _ec_count(value) % _int(factor) == 0


@element_count_op("getKnownScalarFactor")
def _ec_get_known_scalar_factor(value, args, env):
    factor = eval_expr(args[0], env)
    if _is_ec(factor):
        factor = _ec_count(factor)
    factor = _int(factor)
    if _ec_count(value) % factor:
        raise EvalError("not a known scalar factor")
    return _ec_count(value) // factor


@element_count_op("getFixedValue")
def _ec_get_fixed_value(value, args, env):
    if _ec_is_scalable(value):
        raise EvalError("cannot take the fixed value of a scalable count")
    return _ec_count(value)


# --- member dispatch -------------------------------------------------------

def _member_call(method, receiver, args, env):
    if is_llt(receiver):
        impl = LLT_QUERY_IMPL.get(method)
        if impl is None:
            entry = _LLT_QUERIES.get(method)
            if entry is None:
                raise EvalError(f"unknown LLT query {method}")
            raise EvalError(
                f"LLT query {method} is {entry['resolution']}, not modeled")
        return impl(receiver, args, env)
    if _is_ec(receiver):
        impl = ELEMENT_COUNT_IMPL.get(method)
        if impl is None:
            raise EvalError(f"unknown ElementCount query {method}")
        return impl(receiver, args, env)
    if receiver == "TargetLowering" and method == "getTargetMachine":
        return "TargetMachine"
    if receiver == "TargetMachine" and method == "getCodeModel":
        value = env.get("__code_model__")
        if value is None:
            raise EvalError("getCodeModel without a target code model")
        return value
    if receiver == "ST" and method == "getTargetLowering":
        return "TargetLowering"
    raise EvalError(f"cannot call {method} on {receiver!r}")


def apply_member(method, receiver, args, env):
    """Apply one LLT/ElementCount member method (public for the domain)."""
    return _member_call(method, receiver, args, env)


def _pow2(value):
    return value > 0 and (value & (value - 1)) == 0


FREE_FUNCTIONS = {
    "isPowerOf2_32": lambda value: _pow2(int(value)),
    "isPowerOf2_64": lambda value: _pow2(int(value)),
    "isPowerOf2": lambda value: _pow2(int(value)),
    "Log2_32": lambda value: int(value).bit_length() - 1,
    "Log2_64": lambda value: int(value).bit_length() - 1,
    "alignTo": lambda value, align: (int(value) + int(align) - 1)
                                   // int(align) * int(align),
    "alignDown": lambda value, align: int(value) // int(align) * int(align),
    "divideCeil": lambda value, divisor: (int(value) + int(divisor) - 1)
                                         // int(divisor),
    "PowerOf2Ceil": lambda value: 1 << (max(1, int(value)) - 1).bit_length(),
}


def eval_call(node, env):
    name = node.name
    if name in FREE_FUNCTIONS:
        args = [eval_expr(arg, env) for arg in node.args]
        return FREE_FUNCTIONS[name](*args)

    if name.endswith(".Types.__index__"):
        idx = _int(eval_expr(node.args[0], env))
        types = env.get("__types__") or []
        if 0 <= idx < len(types):
            return types[idx]
        raise EvalError(f"type index {idx} is out of range")
    if name.endswith(".MMODescrs.__index__.MemoryTy"):
        if env.get("__mem__") is None:
            raise EvalError("no memory type in this query")
        return env["__mem__"]
        if env.get("__mem__") is None:
            raise EvalError("no memory type in this query")
        return env["__mem__"]

    if name.startswith(("Subtarget.", "ST.")) and name.count(".") == 1:
        method = name.split(".", 1)[1]
        feats = env.get("__features__", {})
        if method in feats:
            return feats[method]
        raise EvalError(f"unknown subtarget predicate {method}")

    suffix = name.rsplit(".", 1)[-1]
    if suffix == "getCodeModel":
        value = env.get("__code_model__")
        if value is None:
            raise EvalError("getCodeModel without a target code model")
        return value
    if suffix == "getTargetLowering":
        return "TargetLowering"
    if suffix == "getTargetMachine":
        return "TargetMachine"
    if suffix == "getInstrInfo":
        return "MCInstrInfo"

    if name.startswith("LLT::"):
        ctor = name[len("LLT::"):]
        impl = LLT_CTOR_IMPL.get(ctor)
        if impl is None:
            raise EvalError(f"unknown LLT constructor {ctor}")
        return impl(node.args, env)
    if name.startswith("ElementCount::"):
        op = name[len("ElementCount::"):]
        impl = ELEMENT_COUNT_IMPL.get(op)
        if impl is None:
            raise EvalError(f"unknown ElementCount constructor {op}")
        return impl(None, node.args, env)

    if node.receiver is not None:
        return _member_call(name.rsplit(".", 1)[-1],
                            eval_expr(node.receiver, env), node.args, env)

    # Legacy dotted form (no receiver preserved): resolve the prefix in env.
    if "." in name:
        prefix, method = name.rsplit(".", 1)
        value = env.get(prefix)
        if value is not None:
            return _member_call(method, value, node.args, env)

    if name in ("ElementCount::getFixed", "ElementCount::getScalable"):
        raise EvalError(f"unhandled ElementCount constructor {name}")

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


def lambda_param_name(lam):
    """The LegalityQuery parameter name (``Q``, ``Query``, ...)."""
    if lam is None:
        return "Query"
    names = [tok.text for tok in lex(lam.params) if tok.kind == "id"]
    return names[-1] if names else "Query"


def lambda_has_mem(lam):
    return any(t.text == "MMODescrs" for t in lam.body)


def lambda_has(lam, token_text):
    return any(t.text == token_text for t in lam.body)
