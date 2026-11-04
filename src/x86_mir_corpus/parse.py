"""Parse a hand-written target legalizer constructor into a rule ledger.

The chain-method vocabulary, slot roles, and predicate names come from the
generated :mod:`x86_mir_corpus.dsl` symbol table, not from a hand-maintained
list here.  A method that is not in the table is a hard parse error unless the
caller opts into opaque mode.
"""

from dataclasses import dataclass, field

from . import dsl as _dsl
from . import expr as E
from .lexer import lex, matching

_TABLE = _dsl.load()

# Methods that read a type slot as their first argument.
SLOT0_METHODS = _dsl.slot0_methods(_TABLE)

# Methods whose arguments include one or more legal type lists.
TYPE_LIST_METHODS = _dsl.type_list_methods(_TABLE)

# Each argument (after an optional predicate) is the class list for one type
# slot; the Cartesian product is the query shape.
CARTESIAN_METHODS = _dsl.cartesian_methods(_TABLE)

# Every chain method the DSL exposes.
KNOWN_METHODS = _dsl.method_names(_TABLE)

# Methods that can carry a predicate lambda; used only for the opaque counter.
OPAQUE_METHODS = {entry["name"] for entry in _TABLE["methods"]
                  if entry["predicate_gated"]}

# Methods whose first argument may be a feature predicate.  When it evaluates
# to false the C++ builder adds no rule at all; keeping the rule visible but
# inactive is what makes a per-config coverage report honest.
PRED_GATED_METHODS = OPAQUE_METHODS


@dataclass
class Rule:
    index: int
    method: str
    line: int
    args: list
    arg_text: list
    guard: list = field(default_factory=list)   # [(text, active)], active: bool|None
    evaluated_args: list = field(default_factory=list)

    @property
    def active(self):
        for _, value in self.guard:
            if value is None:
                return None
        if not all(bool(v) for _, v in self.guard):
            return False
        if self.method in PRED_GATED_METHODS and self.evaluated_args:
            first = self.evaluated_args[0]
            if isinstance(first, bool):
                return first
        return True

    def why_inactive(self):
        for text, value in self.guard:
            if value is False:
                return f"guard false: {text}"
        if self.method in PRED_GATED_METHODS and self.evaluated_args:
            first = self.evaluated_args[0]
            if isinstance(first, bool) and not first:
                return "feature predicate false"
        return None


@dataclass
class Builder:
    name: str
    opcodes: list
    line: int
    rules: list = field(default_factory=list)
    slots: set = field(default_factory=set)
    has_mem: bool = False
    mem_classes: set = field(default_factory=set)
    aligns: set = field(default_factory=set)

    @property
    def num_slots(self):
        if self.has_mem:
            return 2
        return max(1, (max(self.slots) + 1) if self.slots else 1)


@dataclass
class ParseResult:
    source: dict
    config: str
    features: dict
    builders: list
    classes: set
    mem_classes: set
    aligns: set
    call_sites: int
    unknown_methods: list
    unknown_predicates: list
    unknown_refs: list
    opaque_lambdas: int

    def census(self):
        opcodes = set()
        for b in self.builders:
            opcodes.update(b.opcodes)
        return {
            "call_sites": self.call_sites,
            "builders": len(self.builders),
            "opcodes": len(opcodes),
            "classes": len(self.classes),
            "mem_classes": len(self.mem_classes),
            "aligns": len(self.aligns),
        }


class ParserState:
    def __init__(self, features):
        self.features = features
        self.builders = []
        self.call_sites = 0
        self.unknown_methods = []
        self.unknown_predicates = []
        self.unknown_refs = []
        self.early_return = False
        self.opaque_lambdas = 0
        self.mem_classes = set()
        self.aligns = set()


_PREDICATE_IGNORE = frozenset({
    "static_cast", "std", "LLT", "LegalityQuery",
    # Functional casts from a lambda to the predicate/mutation typedef.
    "LegalityPredicate", "LegalizeMutation",
})
_DSL_CALLS = (_dsl.vocabulary_names(_TABLE) | {"all", "any", "predNot"}
              | _PREDICATE_IGNORE)


def _predicate_names(args):
    names = set()
    for arg in args:
        if isinstance(arg, (E.Lambda, E.Unknown)):
            continue
        for name in E.call_names(arg):
            if name.startswith(("LLT::", "std::")) or "." in name:
                continue
            names.add(name.rsplit("::", 1)[-1])
    return names


def parse_source(src, source, config="C2", features=None,
                 rule_set_prefix="X86_", llt_extended=False):
    toks = lex(src)
    state = ParserState(features or {})
    state.rule_set_prefix = rule_set_prefix
    body = find_constructor(toks)
    if body is None:
        raise E.ExprError("cannot find the legalizer constructor body")
    start, end = body
    env = {"__features__": state.features, "__llt_extended__": llt_extended,
           "__code_model__": state.features.get("codeModel")}
    for value in ("Tiny", "Small", "Kernel", "Large", "LargePIC"):
        env[f"CodeModel::{value}"] = value
    env.update(state.features)
    bp = BodyParser(toks, state)
    bp.parse_statements(start + 1, end, env, [])

    classes = scan_all_types(toks, start + 1, end, env)
    classes.discard(None)
    # The no-rule builder case still needs a class axis; p0 is always present.
    classes.add(E.ptr(0))

    result = ParseResult(
        source=source,
        config=config,
        features=state.features,
        builders=state.builders,
        classes=classes,
        mem_classes=state.mem_classes,
        aligns=state.aligns,
        call_sites=count_calls(toks, start + 1, end),
        unknown_methods=state.unknown_methods,
        unknown_predicates=state.unknown_predicates,
        unknown_refs=state.unknown_refs,
        opaque_lambdas=state.opaque_lambdas,
    )
    finalize(result)
    return result


def count_calls(toks, start=0, end=None):
    end = len(toks) if end is None else end
    return sum(1 for t in toks[start:end] if t.text == "getActionDefinitionsBuilder")


def find_constructor(toks):
    for i in range(len(toks) - 4):
        if (toks[i].kind == "id" and toks[i + 1].text == "::"
                and toks[i + 2].kind == "id"
                and toks[i + 2].text == toks[i].text
                and toks[i + 3].text == "("):
            close = matching(toks, i + 3)
            if close < 0:
                continue
            j = close + 1
            while j < len(toks) and toks[j].text not in ("{", ";", "", "}"):
                j += 1
            if j >= len(toks) or toks[j].text != "{":
                continue
            bclose = matching(toks, j)
            if bclose < 0:
                continue
            if any(t.text == "getActionDefinitionsBuilder" for t in toks[j:bclose]):
                return j, bclose
    return None


def scan_all_types(toks, start, end, env):
    """Every LLT the constructor mentions, including untaken guards.

    Alias declarations give one value per alias; identifiers are looked up in
    env.  LLT::... expressions are evaluated in place.  This keeps the type
    axis independent of the configuration even when an `if` prunes a branch.
    """
    out = set()
    i = start
    while i < end:
        if (toks[i].text == "LLT" and i + 3 < end and toks[i + 1].text == "::"
                and toks[i + 2].kind == "id" and toks[i + 3].text == "("):
            close = matching(toks, i + 3, end)
            if close < 0:
                i += 1
                continue
            try:
                node = E.Parser(toks, i, close + 1).parse_expr()
                v = E.eval_expr(node, env)
                if E.is_llt(v):
                    out.add(v)
            except E.ExprError:
                pass
            i = close + 1
            continue
        t = toks[i]
        if t.kind == "id" and E.is_llt(env.get(t.text)):
            out.add(env[t.text])
        i += 1
    return out


class BodyParser:
    def __init__(self, toks, state):
        self.toks = toks
        self.state = state

    # -- statements ---------------------------------------------------------
    def parse_statements(self, i, end, env, guards):
        while i < end:
            if self.state.early_return:
                break
            if self.toks[i].text == "}":
                break
            nxt = self.parse_one(i, end, env, guards)
            i = nxt if nxt > i else i + 1
        return i

    def parse_one(self, i, end, env, guards):
        t = self.toks[i]
        if t.text == "if":
            return self.parse_if(i, end, env, guards)
        if t.text == "for":
            return self.parse_for(i, end, env, guards)
        if t.text == "auto" and i + 1 < end and self.toks[i + 1].text == "&":
            return self.parse_auto_builder(i, end, env, guards)
        if t.text == "getActionDefinitionsBuilder":
            return self.parse_builder_call(i, end, env, guards)
        if (t.kind == "id" and i + 1 < end and self.toks[i + 1].text == "."
                and isinstance(env.get(t.text), Builder)):
            return self.parse_action_chain(i, end, env, env[t.text], guards)
        return self.parse_decl_or_skip(i, end, env, guards)

    def parse_if(self, i, end, env, guards):
        cond_open = i + 1
        if self.toks[cond_open].text != "(":
            return skip_statement(self.toks, i, end)
        cond_close = matching(self.toks, cond_open, end)
        if cond_close < 0:
            return end
        cond_text = " ".join(t.text for t in self.toks[cond_open + 1:cond_close])
        value = eval_safe_tokens(self.toks[cond_open + 1:cond_close], env)
        # A definite condition is pruned: this is how the LOAD/STORE else branch
        # and the config-specific legalFor lists stay out of the wrong builder.
        then_guards = [*guards, (cond_text, value)]
        k = cond_close + 1
        if self.is_return_branch(k, end):
            # ``if (!ST.hasNEON()) return;`` disables the rest of the
            # constructor.  A taken return stops the parse; an undecidable
            # guard leaves every following rule undecided.
            if value is True:
                self.state.early_return = True
                return end
            if value is None:
                guards.append(("early return (" + cond_text + ")", None))
                return k
        k = self.parse_branch(k, end, env, then_guards, take=value is not False)
        if k < end and self.toks[k].text == "else":
            k += 1
            other_guards = [
                *guards,
                ("!(" + cond_text + ")", None if value is None else not value),
            ]
            k = self.parse_branch(k, end, env, other_guards, take=value is not True)
        return k

    def is_return_branch(self, i, end):
        if i >= end:
            return False
        if self.toks[i].text == "return":
            return True
        if self.toks[i].text != "{":
            return False
        close = matching(self.toks, i, end)
        if close < 0:
            return False
        body = [t for t in self.toks[i + 1:close] if t.text != ";"]
        return len(body) == 1 and body[0].text == "return"

    def parse_branch(self, i, end, env, guards, take):
        if i >= end:
            return i
        if self.toks[i].text == "{":
            close = matching(self.toks, i, end)
            if close < 0:
                return end
            if take:
                self.parse_statements(i + 1, close, env, guards)
            return close + 1
        if take:
            return self.parse_one(i, end, env, guards)
        return skip_statement(self.toks, i, end)

    def parse_for(self, i, end, env, guards):
        open_i = i + 1
        if self.toks[open_i].text != "(":
            return skip_statement(self.toks, i, end)
        close = matching(self.toks, open_i, end)
        if close < 0:
            return end
        inner = self.toks[open_i + 1:close]
        colon = next((k for k, t in enumerate(inner) if t.text == ":"), -1)
        if colon < 0 or "unsigned" not in [t.text for t in inner[:colon]]:
            return skip_statement(self.toks, i, end)
        var = inner[colon - 1].text
        # The range must be a braced list of opcode identifiers.
        rng = inner[colon + 1:]
        if not rng or rng[0].text != "{":
            return skip_statement(self.toks, i, end)
        rclose = next((k for k in range(len(rng)) if rng[k].text == "}"), -1)
        if rclose < 0:
            return skip_statement(self.toks, i, end)
        values = []
        for t in rng[1:rclose]:
            if t.kind == "id" and t.text.startswith("G_"):
                values.append(t.text)
        if not values:
            return skip_statement(self.toks, i, end)
        body_i = close + 1
        if body_i >= end or self.toks[body_i].text != "{":
            return skip_statement(self.toks, i, end)
        body_end = matching(self.toks, body_i, end)
        if body_end < 0:
            return end
        for opcode in values:
            local = dict(env)
            local[var] = E.Opcode(opcode)
            self.parse_statements(body_i + 1, body_end, local, guards)
        return body_end + 1

    def parse_auto_builder(self, i, end, env, guards):
        # auto &Action = getActionDefinitionsBuilder(Op);
        j = i + 2
        if j >= end or self.toks[j].kind != "id":
            return skip_statement(self.toks, i, end)
        name = self.toks[j].text
        j += 1
        if j >= end or self.toks[j].text != "=":
            return skip_statement(self.toks, i, end)
        j += 1
        if j >= end or self.toks[j].text != "getActionDefinitionsBuilder":
            return skip_statement(self.toks, i, end)
        builder, j = self.make_builder(j, end, env, guards)
        env[name] = builder
        if j < end and self.toks[j].text == ";":
            j += 1
        return j

    def parse_builder_call(self, i, end, env, guards):
        builder, j = self.make_builder(i, end, env, guards)
        return j

    def make_builder(self, i, end, env, guards):
        assert self.toks[i].text == "getActionDefinitionsBuilder"
        open_i = i + 1
        if open_i >= end or self.toks[open_i].text != "(":
            raise E.ExprError(f"getActionDefinitionsBuilder without '(' at line {self.toks[i].line}")
        close = matching(self.toks, open_i, end)
        if close < 0:
            raise E.ExprError("unterminated getActionDefinitionsBuilder at line "
                              f"{self.toks[i].line}")
        try:
            node = E.Parser(self.toks, open_i + 1, close).parse_expr()
            value = E.eval_expr(node, env)
        except E.ExprError as err:
            raise E.ExprError(
                f"bad opcode argument at line {self.toks[i].line}: {err}") from err
        opcodes = opcode_list(value)
        if not opcodes:
            raise E.ExprError(f"empty opcode set at line {self.toks[i].line}")
        builder = Builder(
            name=self.state.rule_set_prefix + opcodes[0][2:],
            opcodes=opcodes,
            line=self.toks[i].line,
        )
        self.state.builders.append(builder)
        self.state.call_sites += 1
        j = close + 1
        # The immediate chain may continue on the same statement.
        j = self.parse_chain(j, end, env, builder, guards)
        if j < end and self.toks[j].text == ";":
            j += 1
        return builder, j

    def parse_chain(self, i, end, env, builder, guards):
        while i < end and self.toks[i].text == ".":
            i, _ = self.parse_method(i, end, env, builder, guards)
        return i

    def parse_action_chain(self, i, end, env, builder, guards):
        j = i + 1
        j = self.parse_chain(j, end, env, builder, guards)
        if j < end and self.toks[j].text == ";":
            j += 1
        return j

    def parse_method(self, i, end, env, builder, guards):
        # toks[i] == '.'
        if i + 2 >= end or self.toks[i + 1].kind != "id" or self.toks[i + 2].text != "(":
            return skip_statement(self.toks, i, end), None
        method = self.toks[i + 1].text
        line = self.toks[i + 1].line
        open_i = i + 2
        close = matching(self.toks, open_i, end)
        if close < 0:
            return end, None
        args = parse_args(self.toks, open_i, close, env)
        evaluated = []
        for arg in args:
            if isinstance(arg, (E.Lambda, E.Unknown)):
                evaluated.append(None)
                continue
            try:
                evaluated.append(E.eval_expr(arg, env))
            except E.ExprError:
                evaluated.append(None)
        rule = Rule(
            index=len(builder.rules),
            method=method,
            line=line,
            args=args,
            arg_text=[" ".join(t.text for t in self.toks[open_i + 1:close])],
            guard=list(guards),
            evaluated_args=evaluated,
        )
        builder.rules.append(rule)
        if method not in KNOWN_METHODS:
            self.state.unknown_methods.append((method, line))
        for name in _predicate_names(args):
            if name in _DSL_CALLS:
                continue
            self.state.unknown_predicates.append((name, line))
        if method in OPAQUE_METHODS:
            self.state.opaque_lambdas += sum(1 for a in args if isinstance(a, E.Lambda))
        try:
            self.record_shape(builder, method, args, env)
        except E.ExprError as err:
            self.state.unknown_refs.append((method, line, str(err)))
        return close + 1, rule

    def record_shape(self, builder, method, args, env):
        slots = set()
        self._env = env
        for a in args:
            self._collect_slots(a, slots)
        if method in SLOT0_METHODS and args:
            idx = eval_int(args[0], env)
            if idx is not None:
                slots.add(idx)
        if method == "scalarizeIf" and len(args) >= 2:
            idx = eval_int(args[1], env)
            if idx is not None:
                slots.add(idx)
        if method == "scalarSameSizeAs":
            for a in args:
                idx = eval_int(a, env)
                if idx is not None:
                    slots.add(idx)
        if method in CARTESIAN_METHODS:
            slot_lists = [a for a in args if eval_list(a, env) is not None]
            slots.update(range(len(slot_lists)))
        if method in TYPE_LIST_METHODS | {"lowerFor", "lowerIf", "legalIf",
                                          "customIf"}:
            for a in args:
                lst = eval_list(a, env)
                if lst is not None:
                    if lst and isinstance(lst[0], list):
                        slots.update(range(len(lst[0])))
                    else:
                        slots.add(0)
        if method == "legalForTypesWithMemDesc":
            builder.has_mem = True
            slots.update((0, 1))
            for a in args:
                lst = eval_list(a, env)
                if lst is None:
                    continue
                for item in lst:
                    if isinstance(item, list) and len(item) == 4 and E.is_llt(item[2]):
                        builder.mem_classes.add(item[2])
                        self.state.mem_classes.add(item[2])
                        try:
                            align = int(item[3])
                            builder.aligns.add(align)
                            self.state.aligns.add(align)
                        except (TypeError, ValueError):
                            pass
        for a in args:
            if isinstance(a, E.Lambda) and E.lambda_has_mem(a):
                builder.has_mem = True
        builder.slots.update(slots)

    def _collect_slots(self, node, out):
        """Slot indices from predicate calls and lambda Types[i] reads."""
        if not hasattr(self, "_env"):
            self._env = {}
        if isinstance(node, E.Call):
            if node.name in E._PRED_SLOT1 and node.args:
                idx = eval_int(node.args[0], self._env)
                if idx is not None:
                    out.add(idx)
            if node.name in E._PRED_SLOT2 and len(node.args) >= 2:
                for a in node.args[:2]:
                    idx = eval_int(a, self._env)
                    if idx is not None:
                        out.add(idx)
            for a in node.args:
                self._collect_slots(a, out)
        elif isinstance(node, E.Lambda):
            body = node.body
            k = 0
            while k < len(body):
                t = body[k]
                if t.text == "Types" and k + 1 < len(body) and body[k + 1].text == "[":
                    v = body[k + 2] if k + 2 < len(body) else None
                    if v is not None and v.kind == "num":
                        out.add(int(v.text))
                    elif v is not None and v.kind == "id" and isinstance(
                            self._env.get(v.text), int):
                        out.add(self._env[v.text])
                    k += 2
                elif t.text in E._PRED_SLOT1 and k + 2 < len(body) and body[k + 1].text == "(":
                    # ``X.isVector()`` is an LLT method, not the DSL predicate.
                    if k and body[k - 1].text in (".", "->"):
                        k += 1
                        continue
                    close = matching(body, k + 1)
                    if close > k + 2:
                        try:
                            arg = E.Parser(body, k + 2, close).parse_expr()
                        except E.ExprError:
                            arg = None
                        idx = eval_int(arg, self._env) if arg is not None else None
                        # A lambda-local index (SubIdx/FullIdx) is not in env;
                        # the predicate still reads a slot, so keep 0 and 1.
                        out.update((idx,) if idx is not None else (0, 1))
                        k = close
                elif t.text in E._PRED_SLOT2 and k + 2 < len(body) and body[k + 1].text == "(":
                    if k and body[k - 1].text in (".", "->"):
                        k += 1
                        continue
                    close = matching(body, k + 1)
                    if close > k + 2:
                        try:
                            args = parse_args(body, k + 1, close, self._env)
                        except E.ExprError:
                            args = []
                        for a in args[:2]:
                            idx = eval_int(a, self._env)
                            out.update((idx,) if idx is not None else (0, 1))
                        k = close
                k += 1
        elif isinstance(node, E.ListLit):
            for x in node.items:
                self._collect_slots(x, out)
        elif isinstance(node, E.Unary):
            self._collect_slots(node.x, out)
        elif isinstance(node, E.Binary):
            self._collect_slots(node.left, out)
            self._collect_slots(node.right, out)
        elif isinstance(node, E.Ternary):
            self._collect_slots(node.cond, out)
            self._collect_slots(node.then, out)
            self._collect_slots(node.other, out)

    def parse_decl_or_skip(self, i, end, env, guards):
        # Find the top-level '=' before the terminating ';'.
        j = i
        depth = 0
        eq = -1
        while j < end:
            t = self.toks[j].text
            if t in ("(", "[", "{"):
                depth += 1
            elif t in (")", "]", "}"):
                if depth == 0:
                    break
                depth -= 1
            elif t == "=" and depth == 0:
                eq = j
            elif t == ";" and depth == 0:
                break
            j += 1
        if eq < 0 or j >= end or self.toks[j].text != ";":
            return skip_statement(self.toks, i, end)
        name_tok = self.toks[eq - 1] if eq > i else None
        if name_tok is None or name_tok.kind != "id":
            return skip_statement(self.toks, i, end)
        name = name_tok.text
        try:
            node = E.Parser(self.toks, eq + 1, j).parse_expr()
            value = E.eval_expr(node, env)
        except E.ExprError as err:
            value = None
            self.state.unknown_refs.append((name, self.toks[i].line, str(err)))
        env[name] = value
        return j + 1


def parse_args(toks, open_i, close, env):
    args = []
    p = E.Parser(toks, open_i + 1, close)
    while p.i < close:
        start = p.i
        try:
            args.append(p.parse_expr())
        except E.ExprError:
            p.i = start
            depth = 0
            while p.i < close:
                t = p.peek().text
                if t in ("(", "[", "{"):
                    depth += 1
                elif t in (")", "]", "}"):
                    if depth == 0:
                        break
                    depth -= 1
                elif t == "," and depth == 0:
                    break
                p.i += 1
            args.append(E.Unknown(" ".join(x.text for x in toks[start:p.i])))
        if p.i < close and p.peek().text == ",":
            p.i += 1
            continue
        break
    return args


def opcode_list(value):
    out = []
    if isinstance(value, E.Opcode):
        out.append(value.name)
    elif isinstance(value, list):
        for v in value:
            if isinstance(v, E.Opcode):
                out.append(v.name)
            elif isinstance(v, str) and v.startswith("G_"):
                out.append(v)
    elif isinstance(value, str) and value.startswith("G_"):
        out.append(value)
    # dedupe, keep order
    seen = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def eval_safe_tokens(toks, env):
    try:
        node = E.Parser(toks, 0, len(toks)).parse_expr()
        return E.eval_expr(node, env)
    except E.ExprError:
        return None


def eval_int(node, env=None):
    try:
        v = E.eval_expr(node, env or {})
    except E.ExprError:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    return None


def eval_list(node, env=None):
    try:
        v = E.eval_expr(node, env or {})
    except E.ExprError:
        return None
    return v if isinstance(v, list) else None


def skip_statement(toks, i, end):
    depth = 0
    while i < end:
        t = toks[i].text
        if t == "{":
            if depth == 0:
                close = matching(toks, i, end)
                return end if close < 0 else close + 1
            depth += 1
        elif t in ("(", "["):
            depth += 1
        elif t in (")", "]", "}"):
            depth = max(0, depth - 1)
        elif t == ";" and depth == 0:
            return i + 1
        i += 1
    return end


def finalize(result):
    for b in result.builders:
        if b.has_mem:
            b.slots.update((0, 1))
        if not b.slots:
            b.slots.add(0)
    # memdesc alignments are a target property; a source with no memdesc still
    # keeps the reviewed {1} threshold so the align axis is not empty.
    if not result.aligns:
        result.aligns.add(1)
