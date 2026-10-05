"""Tokenizer and recursive-descent parser for OData common expressions ($filter, $orderby items).

The parser accepts the union of V2 and V4 syntax. Deciding what a version allows is the validator's job, so an
expression like `contains(Name,'x')` parses fine and the validator then rejects it for V2 with a useful message.

Precedence, lowest first, from the operator precedence table of OData V4.01 Part 2 (V2 has the same order
without `has` and `in`): or; and; equality (eq ne); relational (gt ge lt le); additive (add sub);
multiplicative (mul div divby mod); unary (- not); primary (/ navigation, has, in, function calls).
Operators of one level associate to the left. So `not Status eq 'A'` is `(not Status) eq 'A'`, which servers
reject, and `not Status has E'X'` is `not (Status has E'X')`.

Operator, lambda and function names are lowercase here: 4.01 services accept any case, 4.0 services do not,
so lowercase is the only spelling that works everywhere. Uppercase spellings get a parse error that says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Union


class ParseError(Exception):
    def __init__(self, message: str, pos: int) -> None:
        super().__init__(message)
        self.pos = pos


# ------------------------------------------------------------------------------------------------ AST


@dataclass(frozen=True)
class Literal:
    kind: str  # see versions.LITERALS
    raw: str
    value: str  # unquoted content for strings and prefixed literals, raw text otherwise
    pos: int


@dataclass(frozen=True)
class Path:
    segments: tuple[str, ...]
    pos: int

    @property
    def text(self) -> str:
        return "/".join(self.segments)


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple[Node, ...]
    pos: int


@dataclass(frozen=True)
class Lambda:
    path: Path  # the collection
    op: str  # any | all
    var: str | None
    body: Node | None
    pos: int


@dataclass(frozen=True)
class Binary:
    op: str
    left: Node
    right: Node
    pos: int


@dataclass(frozen=True)
class Unary:
    op: str  # not | -
    operand: Node
    pos: int


@dataclass(frozen=True)
class ListExpr:
    items: tuple[Node, ...]
    pos: int


Node = Union[Literal, Path, Call, Lambda, Binary, Unary, ListExpr]  # noqa: UP007 (dataclass forward refs)

# ------------------------------------------------------------------------------------------------ tokens


@dataclass
class Token:
    kind: str
    text: str
    pos: int
    literal: str | None = field(default=None)  # literal kind for LITERAL tokens


_Q = r"'(?:[^']|'')*'"
_TOKENS = [
    ("WS", r"\s+"),
    ("STRING", _Q),
    ("PREFIXED", r"(?i:datetimeoffset|datetime|guid|time|binary|duration|geography|geometry|X)" + _Q),
    ("ENUM", r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+" + _Q),
    ("GUID", r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}(?![\w-])"),
    ("DATETIME", r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?(?![\w:.])"),
    ("DATE", r"\d{4}-\d{2}-\d{2}(?![\w:.-])"),
    ("TIMEOFDAY", r"\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?![\w:.])"),
    ("NUMBER", r"[+-]?(?:\d+\.\d+|\d+)(?:[eE][+-]?\d+)?[mMdDfFlL]?(?![\w.])"),
    ("NANINF", r"(?:NaN|-?INF)(?![\w.])"),
    ("IDENT", r"\$?[A-Za-z_]\w*(?:\.[A-Za-z_*]\w*)*"),
    ("PUNCT", r"[(),/:]"),
    ("MINUS", r"-"),
]
_TOKEN_RE = re.compile("|".join(f"(?P<{k}>{p})" for k, p in _TOKENS))

_PREFIX_KIND = {
    "datetime": "datetime_v2",
    "datetimeoffset": "datetimeoffset_v2",
    "guid": "guid_v2",
    "time": "time_v2",
    "binary": "binary",
    "x": "binary",
    "duration": "duration",
    "geography": "geo",
    "geometry": "geo",
}

EQUALITY = ("eq", "ne")
RELATIONAL = ("gt", "ge", "lt", "le")
ADDITIVE = ("add", "sub")
MULTIPLICATIVE = ("mul", "div", "divby", "mod")
PRIMARY_OPS = ("has", "in")
OPERATORS = (*EQUALITY, *RELATIONAL, *ADDITIVE, *MULTIPLICATIVE, *PRIMARY_OPS, "and", "or", "not")


def _number_kind(text: str) -> str:
    suffix = text[-1].lower()
    if suffix == "l":
        return "int64_suffix"
    if suffix == "m":
        return "decimal_suffix"
    if suffix == "d":
        return "double_suffix"
    if suffix == "f":
        return "single_suffix"
    if "e" in text.lower():
        return "double"
    return "decimal" if "." in text else "int"


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if m is None:
            ch = text[pos]
            if ch == "'":
                raise ParseError("unterminated string literal (a quote inside a string is written '')", pos)
            raise ParseError(f"unexpected character {ch!r}", pos)
        kind, value = m.lastgroup or "", m.group()
        if kind == "WS":
            pos = m.end()
            continue
        if kind == "STRING":
            tokens.append(Token("LITERAL", value, pos, "string"))
        elif kind == "PREFIXED":
            prefix = value[: value.index("'")].lower()
            tokens.append(Token("LITERAL", value, pos, _PREFIX_KIND[prefix]))
        elif kind == "ENUM":
            tokens.append(Token("LITERAL", value, pos, "enum"))
        elif kind == "GUID":
            tokens.append(Token("LITERAL", value, pos, "guid"))
        elif kind == "DATETIME":
            has_offset = value.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}$", value) is not None
            tokens.append(Token("LITERAL", value, pos, "datetimeoffset" if has_offset else "datetime_no_offset"))
        elif kind == "DATE":
            tokens.append(Token("LITERAL", value, pos, "date"))
        elif kind == "TIMEOFDAY":
            tokens.append(Token("LITERAL", value, pos, "timeofday"))
        elif kind == "NUMBER":
            tokens.append(Token("LITERAL", value, pos, _number_kind(value)))
        elif kind == "NANINF":
            tokens.append(Token("LITERAL", value, pos, "nan_inf"))
        elif kind == "IDENT":
            low = value.lower()
            if low in ("true", "false"):
                tokens.append(Token("LITERAL", value, pos, "bool"))
            elif value == "null":
                tokens.append(Token("LITERAL", value, pos, "null"))
            elif low == "null":
                raise ParseError(f"write null in lowercase, not {value}", pos)
            else:
                tokens.append(Token("IDENT", value, pos))
        else:
            tokens.append(Token(kind, value, pos))
        pos = m.end()
    tokens.append(Token("EOF", "", len(text)))
    return tokens


def _literal(tok: Token) -> Literal:
    raw = tok.text
    value = raw
    if "'" in raw:
        inner = raw[raw.index("'") + 1 : -1]
        value = inner.replace("''", "'")
    return Literal(tok.literal or "string", raw, value, tok.pos)


# ------------------------------------------------------------------------------------------------ parser


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.toks = tokenize(text)
        self.i = 0

    def peek(self, k: int = 0) -> Token:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def next(self) -> Token:
        t = self.toks[self.i]
        self.i += 1
        return t

    def is_kw(self, *words: str) -> bool:
        t = self.peek()
        return t.kind == "IDENT" and t.text in words

    def is_punct(self, ch: str, k: int = 0) -> bool:
        t = self.peek(k)
        return t.kind == "PUNCT" and t.text == ch

    def expect_punct(self, ch: str) -> Token:
        t = self.next()
        if t.kind != "PUNCT" or t.text != ch:
            raise ParseError(f"expected {ch!r} but found {t.text or 'end of input'!r}", t.pos)
        return t

    # grammar ---------------------------------------------------------------------------------
    def parse(self) -> Node:
        if self.peek().kind == "EOF":
            raise ParseError("empty expression", 0)
        node = self.or_expr()
        t = self.peek()
        if t.kind != "EOF":
            if t.kind == "IDENT" and t.text.lower() in OPERATORS:
                raise ParseError(f"write the operator {t.text!r} in lowercase: {t.text.lower()!r}", t.pos)
            raise ParseError(f"unexpected {t.text!r}", t.pos)
        return node

    def or_expr(self) -> Node:
        left = self.and_expr()
        while self.is_kw("or"):
            t = self.next()
            left = Binary("or", left, self.and_expr(), t.pos)
        return left

    def and_expr(self) -> Node:
        left = self.eq_expr()
        while self.is_kw("and"):
            t = self.next()
            left = Binary("and", left, self.eq_expr(), t.pos)
        return left

    def eq_expr(self) -> Node:
        left = self.rel_expr()
        while self.is_kw(*EQUALITY):
            t = self.next()
            left = Binary(t.text, left, self.rel_expr(), t.pos)
        return left

    def rel_expr(self) -> Node:
        left = self.add_expr()
        while self.is_kw(*RELATIONAL):
            t = self.next()
            left = Binary(t.text, left, self.add_expr(), t.pos)
        return left

    def add_expr(self) -> Node:
        left = self.mul_expr()
        while self.is_kw(*ADDITIVE):
            t = self.next()
            left = Binary(t.text, left, self.mul_expr(), t.pos)
        return left

    def mul_expr(self) -> Node:
        left = self.unary()
        while self.is_kw(*MULTIPLICATIVE):
            t = self.next()
            left = Binary(t.text, left, self.unary(), t.pos)
        return left

    def unary(self) -> Node:
        if self.peek().kind == "MINUS":
            t = self.next()
            return Unary("-", self.unary(), t.pos)
        if self.is_kw("not") and not self.is_punct("/", 1):
            t = self.next()
            return Unary("not", self.unary(), t.pos)
        return self.primary_ops()

    def primary_ops(self) -> Node:
        """`has` and `in` are primary operators: `not A has X` is `not (A has X)`."""
        left = self.primary()
        while self.is_kw(*PRIMARY_OPS):
            t = self.next()
            right = self.primary()
            if t.text == "in" and not isinstance(right, ListExpr):
                right = ListExpr((right,), right.pos) if isinstance(right, Literal) else right
            left = Binary(t.text, left, right, t.pos)
        return left

    def primary(self) -> Node:
        t = self.peek()
        if t.kind == "LITERAL":
            self.next()
            return _literal(t)
        if self.is_punct("("):
            self.next()
            first = self.or_expr()
            if self.is_punct(","):
                items = [first]
                while self.is_punct(","):
                    self.next()
                    items.append(self.or_expr())
                self.expect_punct(")")
                return ListExpr(tuple(items), t.pos)
            self.expect_punct(")")
            return first
        if t.kind == "IDENT":
            if self.is_punct("(", 1):
                return self.call()
            return self.path()
        raise ParseError(f"expected a value but found {t.text or 'end of input'!r}", t.pos)

    def call(self) -> Node:
        name = self.next()
        self.expect_punct("(")
        args: list[Node] = []
        if not self.is_punct(")"):
            args.append(self.or_expr())
            while self.is_punct(","):
                self.next()
                args.append(self.or_expr())
        self.expect_punct(")")
        return Call(name.text, tuple(args), name.pos)

    def path(self) -> Node:
        first = self.next()
        segs = [first.text]
        while self.is_punct("/"):
            self.next()
            t = self.next()
            if t.kind != "IDENT":
                raise ParseError(f"expected a property name after '/' but found {t.text or 'end of input'!r}", t.pos)
            if t.text.lower() in ("any", "all") and self.is_punct("("):
                if t.text not in ("any", "all"):
                    raise ParseError(f"write the lambda operator in lowercase: {t.text.lower()}(...)", t.pos)
                return self.lambda_(Path(tuple(segs), first.pos), t)
            segs.append(t.text)
        return Path(tuple(segs), first.pos)

    def lambda_(self, path: Path, op: Token) -> Node:
        self.expect_punct("(")
        if self.is_punct(")"):
            if op.text == "all":
                raise ParseError("all() needs a variable and a condition: all(x: x/... eq ...)", op.pos)
            self.next()
            return Lambda(path, op.text, None, None, op.pos)
        var = self.next()
        if var.kind != "IDENT":
            raise ParseError(f"expected a lambda variable but found {var.text!r}", var.pos)
        self.expect_punct(":")
        body = self.or_expr()
        self.expect_punct(")")
        return Lambda(path, op.text, var.text, body, op.pos)


def parse(text: str) -> Node:
    return _Parser(text).parse()


# ------------------------------------------------------------------------------------------------ list splitting


def split_top_level(text: str, sep: str) -> list[str]:
    """Split on `sep` outside quotes and parentheses: "a,b(c,d),'x,y'" → ["a", "b(c,d)", "'x,y'"]."""
    parts: list[str] = []
    depth, quoted, start = 0, False, 0
    i = 0
    while i < len(text):
        ch = text[i]
        if quoted:
            if ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    i += 1
                else:
                    quoted = False
        elif ch == "'":
            quoted = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return [p.strip() for p in parts]


def walk(node: Node):
    """Yield every node in the tree, parents first."""
    yield node
    if isinstance(node, Binary):
        yield from walk(node.left)
        yield from walk(node.right)
    elif isinstance(node, Unary):
        yield from walk(node.operand)
    elif isinstance(node, Call | ListExpr):
        for a in node.args if isinstance(node, Call) else node.items:
            yield from walk(a)
    elif isinstance(node, Lambda):
        yield node.path
        if node.body is not None:
            yield from walk(node.body)
