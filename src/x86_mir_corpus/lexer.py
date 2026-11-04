"""Tokenizer for the literal C++ subset used by X86LegalizerInfo.cpp."""

from dataclasses import dataclass


@dataclass
class Token:
    kind: str          # id, num, str, char, punct, pp, eof
    text: str
    line: int
    col: int

    def __repr__(self):
        return f"{self.kind}:{self.text}@{self.line}"


_MULTI = (
    "::", "->", "==", "!=", "<=", ">=", "&&", "||", "<<", ">>",
    "++", "--", "+=", "-=", "*=", "/=", "&=", "|=", "^=", "...",
)


def lex(src):
    toks = []
    i = 0
    line = 1
    col = 1
    n = len(src)

    def advance(seg):
        nonlocal line, col
        if "\n" in seg:
            line += seg.count("\n")
            col = len(seg.rsplit("\n", 1)[-1]) + 1
        else:
            col += len(seg)

    while i < n:
        c = src[i]
        if c in " \t\r":
            i += 1
            col += 1
            continue
        if c == "\n":
            i += 1
            line += 1
            col = 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            advance(src[i:j])
            i = j
            continue
        if c == "#":
            j = src.find("\n", i)
            j = n if j < 0 else j
            toks.append(Token("pp", src[i:j].strip(), line, col))
            advance(src[i:j])
            i = j
            continue
        if c in ('"', "'"):
            quote = c
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == quote:
                    j += 1
                    break
                if src[j] == "\n":
                    break
                j += 1
            toks.append(Token("str" if quote == '"' else "char", src[i:j], line, col))
            advance(src[i:j])
            i = j
            continue
        if c.isdigit():
            j = i
            if src.startswith(("0x", "0X"), i):
                j = i + 2
                while j < n and src[j] in "0123456789abcdefABCDEF":
                    j += 1
            elif src.startswith(("0b", "0B"), i):
                j = i + 2
                while j < n and src[j] in "01":
                    j += 1
            else:
                while j < n and src[j].isdigit():
                    j += 1
            while j < n and src[j] in "uUlL":
                j += 1
            toks.append(Token("num", src[i:j], line, col))
            advance(src[i:j])
            i = j
            continue
        if c.isalpha() or c == "_":
            j = i + 1
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            toks.append(Token("id", src[i:j], line, col))
            advance(src[i:j])
            i = j
            continue
        for op in _MULTI:
            if src.startswith(op, i):
                toks.append(Token("punct", op, line, col))
                advance(op)
                i += len(op)
                break
        else:
            toks.append(Token("punct", c, line, col))
            advance(c)
            i += 1

    toks.append(Token("eof", "", line, col))
    return toks


def matching(toks, open_index, end_index=None):
    """Index of the token matching toks[open_index] for () [] {}.  -1 if none."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    opening = toks[open_index].text
    closing = pairs.get(opening)
    if closing is None:
        return -1
    depth = 0
    limit = len(toks) if end_index is None else end_index
    for i in range(open_index, limit):
        t = toks[i].text
        if t == opening:
            depth += 1
        elif t == closing:
            depth -= 1
            if depth == 0:
                return i
    return -1
