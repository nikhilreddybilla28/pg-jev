"""Deterministic checks for a candidate query. Nothing reaches Jev or the caller without passing here.

`validate(service, entity_set, query_options, version)`:

1. normalises option keys and value types (`filter` → `$filter`, `"10"` → 10, lists joined, `$count` ↔
   `$inlinecount` and `$search` ↔ SAP `search` mapped to the target version), each reported as a `fixed` issue;
2. checks the entity set exists and is addressable;
3. parses `$filter`, `$orderby`, `$select` and `$expand` and resolves every path against the full metadata,
   through navigation and complex properties (a name that exists nowhere is an error with "did you mean");
4. checks functions, operators and literal forms against the version, and literal types against property types;
5. applies SAP / Capabilities flags: filterable, sortable, searchable, pageable, countable, required filters.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from typing import Any, Literal

from pydantic import BaseModel, Field

from . import expression as ex
from .metadata import ComplexType, EntitySet, EntityType, NavigationProperty, Property, Service
from .versions import FUNCTIONS, LITERAL_HINT, LITERALS, QUERY_OPTIONS, Version, edm_family, literal_example


class Issue(BaseModel):
    option: str
    message: str
    severity: Literal["error", "warning", "fixed"] = "error"

    def __str__(self) -> str:
        return f"{self.option}: {self.message}"


class Validation(BaseModel):
    entity_set: str | None
    query_options: dict[str, Any]
    issues: list[Issue] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list, description="property paths the query references")

    @property
    def valid(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]


# ------------------------------------------------------------------------------------------------ type info


@dataclass
class TypeInfo:
    family: str  # versions.Family, or "entity" / "complex" / "null" / "any"
    edm: str | None = None
    collection: bool = False
    entity: EntityType | None = None
    complex: ComplexType | None = None
    prop: Property | None = None
    nav: NavigationProperty | None = None
    path: str = ""  # canonical path from the entity set root

    def describe(self) -> str:
        if self.prop is not None:
            return f"{self.path} ({self.prop.type})"
        if self.nav is not None:
            return f"{self.path} (navigation{', collection' if self.collection else ''})"
        return self.family


ANY = TypeInfo("any")
BOOL = TypeInfo("bool")
TEMPORAL = ("date", "datetime", "time")
ARG_FAMILIES = {"temporal": TEMPORAL, "date_part": ("date", "datetime"), "time_part": ("datetime", "time")}
STRING_PAIR = ("substringof", "contains", "startswith", "endswith", "indexof", "matchesPattern", "matchespattern")
NESTED_OPTIONS = {  # what may appear inside Nav(...), Nav/$ref(...), Nav/$count(...) (V4 ABNF expandOption & co.)
    "": ("$select", "$filter", "$orderby", "$top", "$skip", "$expand", "$count", "$levels", "$search"),
    "/$ref": ("$filter", "$search", "$orderby", "$top", "$skip", "$count"),
    "/$count": ("$filter", "$search"),
}

_OPTION_ORDER = (
    "$filter",
    "$select",
    "$expand",
    "$orderby",
    "$top",
    "$skip",
    "$count",
    "$inlinecount",
    "$search",
    "search",
)
_KNOWN = {"filter", "select", "expand", "orderby", "top", "skip", "count", "inlinecount", "search"}

_DT_V2 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,7})?)?$")
_DTO_V2 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,7})?)?(Z|[+-]\d{2}:\d{2})$")
_GUID = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")
_DURATION = re.compile(r"^-?P(\d+D)?(T(\d+H)?(\d+M)?(\d+(\.\d+)?S)?)?$")


def suggest(name: str, candidates: list[str]) -> str:
    same_case = [c for c in candidates if c.lower() == name.lower()]
    if same_case:
        return f" (did you mean {same_case[0]!r}? names are case-sensitive)"
    close = difflib.get_close_matches(name, candidates, n=3, cutoff=0.6)
    if close:
        return " (did you mean " + " or ".join(repr(c) for c in close) + "?)"
    return ""


# ------------------------------------------------------------------------------------------------ checker


class _Checker:
    def __init__(self, service: Service, es: EntitySet, version: Version) -> None:
        self.svc = service
        self.es = es
        self.version = version
        self.issues: list[Issue] = []
        self.fields: dict[str, None] = {}

    def error(self, option: str, message: str) -> None:
        self.issues.append(Issue(option=option, message=message))

    def warn(self, option: str, message: str) -> None:
        self.issues.append(Issue(option=option, message=message, severity="warning"))

    def note(self, path: str) -> None:
        if path:
            self.fields[path] = None

    # -------------------------------------------------------------------------------- paths
    def prop_info(self, p: Property, path: str) -> TypeInfo:
        if p.kind == "complex":
            return TypeInfo("complex", p.type, p.collection, complex=self.svc.complex_type(p.type), prop=p, path=path)
        family = "enum" if p.kind == "enum" else edm_family(p.type)
        return TypeInfo(family, p.type, p.collection, prop=p, path=path)

    def resolve(
        self, path: ex.Path, option: str, root: EntityType, prefix: str, scope: Mapping[str, TypeInfo]
    ) -> TypeInfo | None:
        segs = list(path.segments)
        if segs[0] in scope:
            cur = scope[segs[0]]
            segs = segs[1:]
        else:
            if segs[0] == "$it":
                segs = segs[1:]
            cur = TypeInfo("entity", entity=root, path=prefix)
        for seg in segs:
            if seg == "$count":
                if not cur.collection:
                    self.error(option, f"$count needs a collection, but {cur.path or 'the entity'} is not one")
                    return None
                if self.version == "v2":
                    self.error(option, f"{cur.path}/$count inside an expression is V4 only")
                    return None
                return TypeInfo("numeric", "Edm.Int32", path=f"{cur.path}/$count")
            if cur.collection:
                self.error(option, self.collection_hint(cur, option))
                return None
            child_path = f"{cur.path}/{seg}" if cur.path else seg
            if cur.family == "entity" and cur.entity is not None:
                et = cur.entity
                p = et.property(seg)
                if p is not None:
                    cur = self.prop_info(p, child_path)
                    continue
                n = et.navigation(seg)
                if n is not None:
                    cur = TypeInfo(
                        "entity", collection=n.collection, entity=self.svc.nav_target(n), nav=n, path=child_path
                    )
                    if cur.entity is None:
                        self.error(option, f"navigation property {child_path} leads to an unknown entity type")
                        return None
                    continue
                owner = cur.path or self.owner_name(cur)
                self.error(option, f"unknown property {seg!r} on {owner}{suggest(seg, et.member_names())}")
                return None
            if cur.family == "complex" and cur.complex is not None:
                p = cur.complex.property(seg)
                if p is None:
                    self.error(
                        option,
                        f"unknown property {seg!r} in {cur.path}"
                        f"{suggest(seg, [q.name for q in cur.complex.properties])}",
                    )
                    return None
                cur = self.prop_info(p, child_path)
                continue
            self.error(option, f"{cur.path} is {cur.edm or cur.family} and has no property {seg!r}")
            return None
        return cur

    def owner_name(self, cur: TypeInfo) -> str:
        if cur.entity is self.es.entity_type:
            return self.es.name
        return cur.entity.name.rpartition(".")[2] if cur.entity else "?"

    def collection_hint(self, cur: TypeInfo, option: str) -> str:
        what = f"{cur.path} is a collection"
        if option.startswith("$orderby"):
            return f"{what}; you cannot sort by its members"
        if self.version == "v4":
            return f"{what}; filter on its members with {cur.path}/any(x: x/... eq ...)"
        target = None
        if cur.nav is not None:
            ts = self.svc.nav_target_set(cur.nav, self.es)
            target = ts.name if ts else None
        alt = f" query {target} directly or" if target else ""
        return f"{what} (to-many navigation); V2 cannot filter on it:{alt} use $expand={cur.path}"

    # -------------------------------------------------------------------------------- expressions
    def infer(
        self, node: ex.Node, option: str, root: EntityType, prefix: str, scope: Mapping[str, TypeInfo]
    ) -> TypeInfo:
        if isinstance(node, ex.Literal):
            return self.literal(node, option)
        if isinstance(node, ex.Path):
            if any(s.startswith("Edm.") for s in node.segments):
                return ANY  # a type name argument of cast() / isof()
            info = self.resolve(node, option, root, prefix, scope)
            if info is None:
                return ANY
            self.note(info.path if not info.path.endswith("/$count") else info.path[: -len("/$count")])
            if option.startswith("$filter") and info.prop is not None:
                self.check_filterable(info, option)
            return info
        if isinstance(node, ex.Call):
            return self.call(node, option, root, prefix, scope)
        if isinstance(node, ex.Lambda):
            return self.lambda_(node, option, root, prefix, scope)
        if isinstance(node, ex.Unary):
            inner = self.infer(node.operand, option, root, prefix, scope)
            if node.op == "not":
                if inner.family not in ("bool", "any"):
                    self.error(
                        option,
                        f"'not' binds tighter than comparisons and needs a condition, got "
                        f"{inner.describe()}; write not (... eq ...) or use ne",
                    )
                return BOOL
            if inner.family not in ("numeric", "duration", "any"):
                self.error(option, f"'-' needs a number, got {inner.describe()}")
            return inner
        if isinstance(node, ex.ListExpr):
            self.error(option, "a parenthesised list is only allowed after 'in'")
            return ANY
        assert isinstance(node, ex.Binary)
        return self.binary(node, option, root, prefix, scope)

    def literal(self, lit: ex.Literal, option: str) -> TypeInfo:
        family, versions = LITERALS[lit.kind]
        if self.version not in versions:
            hint = LITERAL_HINT.get((lit.kind, self.version), f"not valid in OData {self.version.upper()}")
            self.error(option, f"literal {lit.raw}: {hint}")
            return TypeInfo(family)
        v = lit.value
        if lit.kind == "datetime_v2" and not _DT_V2.match(v):
            if re.match(r"^\d{4}-\d{2}-\d{2}$", v):
                self.error(option, f"literal {lit.raw}: SAP Gateway needs the time part: datetime'{v}T00:00:00'")
            elif _DTO_V2.match(v):
                self.error(
                    option,
                    f"literal {lit.raw}: datetime'…' has no time zone; drop it, or use "
                    f"datetimeoffset'{v}' for an Edm.DateTimeOffset property",
                )
            else:
                self.error(option, f"literal {lit.raw}: expected datetime'YYYY-MM-DDThh:mm:ss'")
        elif lit.kind == "datetimeoffset_v2" and not _DTO_V2.match(v):
            self.error(option, f"literal {lit.raw}: expected datetimeoffset'YYYY-MM-DDThh:mm:ssZ'")
        elif lit.kind in ("datetime_v2", "datetimeoffset_v2", "datetimeoffset", "date"):
            try:
                text = v.replace("Z", "+00:00")
                (date.fromisoformat if lit.kind == "date" else datetime.fromisoformat)(text)
            except ValueError:
                self.error(option, f"literal {lit.raw} is not a valid date")
        elif lit.kind == "timeofday":
            try:
                time.fromisoformat(v)
            except ValueError:
                self.error(option, f"literal {lit.raw} is not a valid time of day")
        elif lit.kind == "guid_v2" and not _GUID.match(v):
            self.error(option, f"literal {lit.raw} is not a valid GUID")
        elif lit.kind in ("time_v2", "duration") and not _DURATION.match(v):
            self.error(option, f"literal {lit.raw}: expected an ISO 8601 duration such as PT13H20M")
        return TypeInfo(family)

    def call(
        self, node: ex.Call, option: str, root: EntityType, prefix: str, scope: Mapping[str, TypeInfo]
    ) -> TypeInfo:
        f = FUNCTIONS.get(node.name)
        if node.name in ("cast", "isof"):  # the last argument is a type name, not a property path
            args = [ANY if _is_type_name(a) else self.infer(a, option, root, prefix, scope) for a in node.args]
        else:
            args = [self.infer(a, option, root, prefix, scope) for a in node.args]
        if f is None:
            self.error(option, f"unknown function {node.name}(){suggest(node.name, list(FUNCTIONS))}")
            return ANY
        if self.version not in f.versions:
            if node.name == "contains":
                hint = "V2 has no contains(); use substringof('text',Property)"
            elif node.name == "substringof":
                hint = "V4 has no substringof(); use contains(Property,'text')"
            else:
                hint = f"{node.name}() is not available in OData {self.version.upper()}"
            self.error(option, hint)
            return TypeInfo(f.returns)
        if not f.min_args <= len(node.args) <= f.max_args:
            n = f"{f.min_args}" if f.min_args == f.max_args else f"{f.min_args}-{f.max_args}"
            self.error(
                option, f"{node.name}() takes {n} argument{'s' if f.max_args != 1 else ''}, got {len(node.args)}"
            )
            return TypeInfo(f.returns)
        if node.name == "substringof" and isinstance(node.args[0], ex.Path) and isinstance(node.args[1], ex.Literal):
            self.error(
                option,
                f"substringof takes the searched text first: substringof({node.args[1].raw},{node.args[0].text})",
            )
            return BOOL
        if (
            node.name in ("contains", "startswith", "endswith", "indexof")
            and isinstance(node.args[0], ex.Literal)
            and isinstance(node.args[1], ex.Path)
        ):
            self.error(
                option,
                f"{node.name} takes the property first: {node.name}({node.args[1].text},{node.args[0].raw})",
            )
            return TypeInfo(f.returns)
        if node.name in STRING_PAIR:
            for a in args:
                if a.family not in ("string", "any", "null"):
                    self.error(option, f"{node.name}() compares strings, got {a.describe()}")
                    break
        elif f.arg_family and f.subject < len(args):
            subj = args[f.subject]
            want = ARG_FAMILIES.get(f.arg_family, (f.arg_family,))
            if subj.family not in (*want, "any", "null"):
                what = {"date_part": "date", "time_part": "date-time or time-of-day"}.get(f.arg_family, f.arg_family)
                self.error(option, f"{node.name}() needs a {what} argument, got {subj.describe()}")
        if node.name in ("round", "floor", "ceiling") and args:
            return args[0] if args[0].family != "any" else TypeInfo("numeric")
        return TypeInfo(f.returns)

    def lambda_(
        self, node: ex.Lambda, option: str, root: EntityType, prefix: str, scope: Mapping[str, TypeInfo]
    ) -> TypeInfo:
        if self.version == "v2":
            self.error(
                option,
                f"{node.path.text}/{node.op}(...) is V4 only; V2 cannot filter on a to-many "
                "navigation: query the related entity set directly",
            )
            return BOOL
        coll = self.resolve(node.path, option, root, prefix, scope)
        if coll is None:
            return BOOL
        if not coll.collection:
            self.error(option, f"{node.op}() needs a collection, but {coll.path} is single-valued")
            return BOOL
        self.note(coll.path)
        if node.body is None:
            return BOOL
        element = replace(coll, collection=False)
        inner = {**scope, node.var or "": element}
        body = self.infer(node.body, option, root, prefix, inner)
        if body.family not in ("bool", "any"):
            self.error(option, f"the body of {node.op}() must be a condition")
        if not _uses_variable(node.body, node.var or ""):
            self.error(
                option, f"the condition inside {node.op}() must use its variable {node.var}, e.g. {node.var}/Property"
            )
        return BOOL

    def binary(
        self, node: ex.Binary, option: str, root: EntityType, prefix: str, scope: Mapping[str, TypeInfo]
    ) -> TypeInfo:
        op = node.op
        if op in ("and", "or"):
            for side in (node.left, node.right):
                t = self.infer(side, option, root, prefix, scope)
                if t.family not in ("bool", "any"):
                    self.error(option, f"both sides of '{op}' must be conditions, got {t.describe()}")
            return BOOL
        left = self.infer(node.left, option, root, prefix, scope)
        if op == "in":
            if self.version == "v2":
                self.error(option, "'in' is V4 only (OData 4.01); use eq ... or eq ...")
                return BOOL
            self.warn(option, "'in' needs an OData 4.01 service; 4.0 services need eq ... or eq ...")
            if isinstance(node.right, ex.ListExpr):
                for it in node.right.items:
                    if not isinstance(it, ex.Literal):
                        self.error(option, "the list after 'in' takes literal values only")
                        continue
                    self.compare(node.left, left, it, self.literal(it, option), option)
                return BOOL
            coll = self.infer(node.right, option, root, prefix, scope)  # a single expression: a collection
            if coll.family != "any" and not coll.collection:
                self.error(option, f"'in' needs a list (a, b) or a collection, got {coll.describe()}")
            elif coll.family != "any":
                self.compare(node.left, left, node.right, replace(coll, collection=False, prop=None), option)
            return BOOL
        if op == "has":
            if self.version == "v2":
                self.error(option, "'has' is V4 only")
                return BOOL
            if left.family not in ("enum", "any"):
                self.error(option, f"'has' needs an enumeration property on the left, got {left.describe()}")
            if not (isinstance(node.right, ex.Literal) and node.right.kind in ("enum", "string")):
                self.error(option, "'has' needs an enumeration literal on the right, e.g. Namespace.Type'Member'")
            elif left.prop is not None and left.family == "enum":
                self.check_enum(left, node.right, option)
            return BOOL
        right = self.infer(node.right, option, root, prefix, scope)
        if op in ("eq", "ne", "gt", "ge", "lt", "le"):
            self.compare(node.left, left, node.right, right, option)
            if op not in ("eq", "ne") and "null" in (left.family, right.family):
                self.error(option, f"'{op} null' is never true; use eq null or ne null")
            for t, other in ((left, right), (right, left)):
                single_vs_null = op in ("eq", "ne") and other.family == "null" and not t.collection
                if (t.collection or t.family in ("entity", "complex")) and not single_vs_null:
                    self.error(option, f"{t.describe()} cannot be compared; compare one of its properties")
            return BOOL
        return self.arithmetic(op, left, right, option)

    def arithmetic(self, op: str, left: TypeInfo, right: TypeInfo, option: str) -> TypeInfo:
        """Result types of add/sub/mul/div/divby/mod (V4 Part 2, arithmetic operators)."""
        a, b = left.family, right.family
        if {a, b} <= {"numeric", "any"}:
            return TypeInfo("numeric")
        if "any" in (a, b):
            return ANY
        if op in ("add", "sub"):
            if a in ("date", "datetime") and b == "duration":
                return TypeInfo(a)
            if op == "add" and a == "duration" and b in ("date", "datetime"):
                return TypeInfo(b)
            if a == b == "duration":
                return TypeInfo("duration")
            if op == "sub" and a == b and a in ("date", "datetime"):
                return TypeInfo("duration")
        if op in ("mul", "div", "divby") and {a, b} == {"duration", "numeric"} and (op == "mul" or a == "duration"):
            return TypeInfo("duration")
        self.error(option, f"'{op}' is not defined for {left.describe()} and {right.describe()}")
        return ANY

    def compare(self, ln: ex.Node, lt: TypeInfo, rn: ex.Node, rt: TypeInfo, option: str) -> None:
        a, b = lt.family, rt.family
        for prop_t, other_t, other_n in ((lt, rt, rn), (rt, lt, ln)):
            if prop_t.family == "enum" and isinstance(other_n, ex.Literal) and other_n.kind in ("enum", "string"):
                self.check_enum(prop_t, other_n, option)  # 4.01 also accepts the member name as a plain string
                return
            if other_t.family == "enum" and isinstance(other_n, ex.Literal) and prop_t.family not in ("enum", "any"):
                self.error(
                    option, f"{other_n.raw} is an enumeration literal but {prop_t.describe()} is not an enumeration"
                )
                return
            if (
                prop_t.family == "duration"
                and isinstance(other_n, ex.Literal)
                and other_n.kind == "string"
                and self.version == "v4"
                and _DURATION.match(other_n.value)
            ):
                return  # 4.01: the duration prefix is optional
        if self.version == "v2":
            for prop_t, other_n in ((lt, rn), (rt, ln)):
                if prop_t.prop is None or not isinstance(other_n, ex.Literal):
                    continue
                if prop_t.prop.type == "Edm.DateTime" and other_n.kind == "datetimeoffset_v2":
                    self.error(option, f"{prop_t.path} is Edm.DateTime: compare it with datetime'…' (no time zone)")
                    return
                if prop_t.prop.type == "Edm.DateTimeOffset" and other_n.kind == "datetime_v2":
                    self.error(
                        option,
                        f"{prop_t.path} is Edm.DateTimeOffset: compare it with "
                        f"datetimeoffset'{other_n.value}Z' (SAP rejects datetime'…' here)",
                    )
                    return
        if "any" in (a, b) or "null" in (a, b) or a == b:
            self.check_code(ln, lt, rn, rt, option)
            return
        if {a, b} == {"date", "datetime"}:
            if self.version == "v2":
                return
            prop_side = lt if lt.prop else rt
            hint = (
                "compare Edm.Date with a date literal such as 2024-01-31"
                if prop_side.family == "date"
                else "compare Edm.DateTimeOffset with 2024-01-31T00:00:00Z, or use date(Property) eq 2024-01-31"
            )
            self.error(option, f"{lt.describe()} vs {rt.describe()}: {hint}")
            return
        for prop_t, other_t, other_n in ((lt, rt, rn), (rt, lt, ln)):
            if prop_t.prop is not None and isinstance(other_n, ex.Literal):
                example = literal_example(prop_t.prop.type, self.version)
                tip = f"; write the value like {example}" if example else ""
                literal = f"{other_n.raw} is a {other_t.family} literal"
                self.error(option, f"{prop_t.path} is {prop_t.prop.type} but {literal}{tip}")
                return
        self.error(option, f"cannot compare {lt.describe()} with {rt.describe()}")

    def check_enum(self, prop_t: TypeInfo, lit: ex.Literal, option: str) -> None:
        """Enum literal: optional qualified type name that must match, then members (names or int64 values)."""
        if lit.kind == "enum":
            type_name = self.svc.qualify(lit.raw[: lit.raw.index("'")])
            if prop_t.prop is not None and type_name != prop_t.prop.type:
                self.error(option, f"{lit.raw} is a {type_name} literal but {prop_t.path} is {prop_t.prop.type}")
                return
        values = prop_t.prop.values if prop_t.prop is not None else None
        for member in (m.strip() for m in lit.value.split(",")):
            if re.fullmatch(r"[+-]?\d+", member):
                continue  # numeric member value
            if values and member not in values:
                self.error(
                    option,
                    f"{member!r} is not a member of {prop_t.path}; use one of: "
                    + ", ".join(f"{k!r}" for k in list(values)[:12]),
                )

    def check_code(self, ln: ex.Node, lt: TypeInfo, rn: ex.Node, rt: TypeInfo, option: str) -> None:
        """Properties with a closed set of codes (SAP status fields, enums) only compare with one of those codes."""
        for prop_t, other in ((lt, rn), (rt, ln)):
            if prop_t.prop is None or not prop_t.prop.values or not isinstance(other, ex.Literal):
                continue
            if other.kind not in ("string", "enum"):
                continue
            value = other.value
            if value not in prop_t.prop.values:
                allowed = ", ".join(
                    f"{k!r}" + (f" ({m})" if m else "") for k, m in list(prop_t.prop.values.items())[:12]
                )
                self.error(option, f"{value!r} is not a valid value of {prop_t.path}; use one of: {allowed}")

    def check_filterable(self, info: TypeInfo, option: str) -> None:
        if info.prop is None:
            return
        top_level = "/" not in info.path
        if not info.prop.filterable or (top_level and info.path in self.es.non_filterable):
            self.error(option, f"{info.path} is not filterable in this service")

    # -------------------------------------------------------------------------------- query options
    def check_filter(self, text: str, option: str, root: EntityType, prefix: str) -> None:
        try:
            node = ex.parse(text)
        except ex.ParseError as e:
            self.error(option, f"cannot parse at position {e.pos}: {e} in {_excerpt(text, e.pos)}")
            return
        t = self.infer(node, option, root, prefix, {})
        if t.family not in ("bool", "any"):
            self.error(option, f"must be a condition (true/false), got {t.describe()}")

    def check_orderby(self, text: str, option: str, root: EntityType, prefix: str) -> None:
        for item in ex.split_top_level(text, ","):
            if not item:
                self.error(option, "empty item")
                continue
            m = re.match(r"^(.*?)(?:\s+(asc|desc))?$", item, re.I | re.S)
            expr_text = m.group(1) if m else item
            try:
                node = ex.parse(expr_text)
            except ex.ParseError as e:
                self.error(option, f"cannot parse {item!r}: {e}")
                continue
            t = self.infer(node, option, root, prefix, {})
            if t.collection or t.family in ("entity", "complex"):
                self.error(option, f"cannot sort by {t.describe()}")
            elif t.prop is not None:
                top_level = "/" not in t.path
                if not t.prop.sortable or (top_level and t.path in self.es.non_sortable):
                    self.error(option, f"{t.path} is not sortable in this service")

    def check_select(self, text: str, option: str, root: EntityType, prefix: str, expanded: set[str]) -> None:
        for item in ex.split_top_level(text, ","):
            if item in ("*", ""):
                if item == "":
                    self.error(option, "empty item")
                continue
            if item.endswith(".*"):
                continue
            segs = item.split("/")
            if not all(re.fullmatch(r"[A-Za-z_]\w*|\*", s) for s in segs):
                self.error(option, f"{item!r} is not a property path")
                continue
            if segs[-1] == "*":
                segs = segs[:-1]
            cur = TypeInfo("entity", entity=root, path=prefix)
            nav_prefix: list[str] = []
            ok = True
            for i, seg in enumerate(segs):
                child_path = f"{cur.path}/{seg}" if cur.path else seg
                if cur.family == "entity" and cur.entity is not None:
                    et = cur.entity
                    p = et.property(seg)
                    n = et.navigation(seg) if p is None else None
                    if p is not None:
                        cur = self.prop_info(p, child_path)
                    elif n is not None:
                        if i < len(segs) - 1 or item.endswith("/*"):
                            if self.version == "v4":
                                self.error(
                                    option,
                                    f"{item}: in V4 select members of a navigation inside $expand: "
                                    f"$expand={seg}($select=...)",
                                )
                                ok = False
                                break
                            nav_prefix.append(seg)
                            if "/".join(nav_prefix) not in expanded:
                                self.error(
                                    option,
                                    f"{item}: add $expand={'/'.join(nav_prefix)} to select through the navigation",
                                )
                                ok = False
                                break
                        cur = TypeInfo(
                            "entity", collection=False, entity=self.svc.nav_target(n), nav=n, path=child_path
                        )
                    else:
                        owner = self.es.name if et is self.es.entity_type else et.name.rpartition(".")[2]
                        self.error(option, f"unknown property {seg!r} on {owner}{suggest(seg, et.member_names())}")
                        ok = False
                        break
                elif cur.family == "complex" and cur.complex is not None:
                    p = cur.complex.property(seg)
                    if p is None:
                        self.error(option, f"unknown property {seg!r} in {cur.path}")
                        ok = False
                        break
                    cur = self.prop_info(p, child_path)
                else:
                    self.error(option, f"{cur.path} has no property {seg!r}")
                    ok = False
                    break
            if ok:
                self.note(cur.path)

    def check_expand(self, text: str, option: str, root: EntityType, prefix: str, depth: int = 0) -> set[str]:
        """Returns the expanded navigation paths (V2 needs them to validate $select=Nav/Prop)."""
        expanded: set[str] = set()
        for item in ex.split_top_level(text, ","):
            m = re.match(r"^([^()]+?)\s*(?:\((.*)\))?$", item, re.S)
            if not item or m is None:
                self.error(option, f"malformed item {item!r}")
                continue
            path_text, opts = m.group(1).strip(), m.group(2)
            if path_text in ("*", "*/$ref"):
                if self.version == "v2" and path_text != "*":
                    self.error(option, f"{item}: not available in V2")
                for part in ex.split_top_level(opts or "", ";"):
                    key, _, value = part.partition("=")
                    if not part:
                        continue
                    if path_text == "*" and key.strip().lower().lstrip("$") == "levels":
                        self.check_levels(value.strip(), f"{option}(*)")
                    else:
                        self.error(option, f"{item}: only $levels may follow *")
                continue
            suffix = ""
            for s in ("/$ref", "/$count"):
                if path_text.endswith(s) and self.version == "v4":
                    path_text, suffix = path_text[: -len(s)], s
            if self.version == "v2" and opts is not None:
                self.error(
                    option,
                    f"{item}: V2 has no options inside $expand; use $select={path_text}/Property "
                    "and filter the parent entity set",
                )
                continue
            segs = path_text.split("/")
            cur_type: EntityType | None = root
            cur_complex: ComplexType | None = None
            walked: list[str] = []
            ok = True
            for i, seg in enumerate(segs):
                last = i == len(segs) - 1
                n = cur_type.navigation(seg) if cur_type is not None else None
                p = None
                if n is None:
                    p = (
                        cur_complex.property(seg)
                        if cur_complex is not None
                        else (cur_type.property(seg) if cur_type is not None else None)
                    )
                if n is not None:
                    if not last and self.version == "v4":
                        self.error(
                            option,
                            f"{path_text}: V4 nests expansions: $expand={seg}($expand={'/'.join(segs[i + 1 :])})",
                        )
                        ok = False
                        break
                    walked.append(seg)
                    expanded.add("/".join(walked))
                    cur_type, cur_complex = self.svc.nav_target(n), None
                    full = f"{prefix}/{'/'.join(walked)}" if prefix else "/".join(walked)
                    self.note(full)
                elif p is not None and p.kind == "complex" and not last and self.version == "v4":
                    walked.append(seg)
                    cur_type, cur_complex = None, self.svc.complex_type(p.type)
                else:
                    names = (
                        cur_type.member_names()
                        if cur_type
                        else [q.name for q in (cur_complex.properties if cur_complex else [])]
                    )
                    what = "is not a navigation property" if p is not None else "is unknown"
                    nav_names = [x.name for x in cur_type.navigation_properties] if cur_type else []
                    self.error(
                        option,
                        f"{seg!r} {what}{suggest(seg, nav_names or names)}"
                        + (f"; navigation properties: {', '.join(nav_names)}" if nav_names else ""),
                    )
                    ok = False
                    break
            if not ok or opts is None or cur_type is None:
                continue
            nested_prefix = f"{prefix}/{'/'.join(walked)}" if prefix else "/".join(walked)
            self.check_nested(
                opts, f"{option}({path_text}{suffix})", cur_type, nested_prefix, depth, NESTED_OPTIONS[suffix]
            )
        return expanded

    def check_levels(self, value: str, label: str) -> None:
        if not (value == "max" or (value.isdigit() and int(value) >= 1)):
            self.error(label, f"$levels must be a positive integer or max, got {value!r}")

    def check_nested(
        self,
        opts: str,
        option: str,
        et: EntityType,
        prefix: str,
        depth: int,
        allowed: tuple[str, ...] = NESTED_OPTIONS[""],
    ) -> None:
        nested: dict[str, str] = {}
        for part in ex.split_top_level(opts, ";"):
            if not part:
                continue
            key, eq, value = part.partition("=")
            key = key.strip().lower()
            if not key.startswith("$"):
                key = "$" + key
            if not eq:
                self.error(option, f"malformed option {part!r}")
                continue
            nested[key] = value.strip()
        expanded: set[str] = set()
        if "$expand" in nested:
            if depth >= 4:
                self.error(option, "expansions nested deeper than 4 levels")
            else:
                expanded = self.check_expand(nested["$expand"], f"{option}/$expand", et, prefix, depth + 1)
        for key, value in nested.items():
            label = f"{option}/{key}"
            if key not in allowed:
                self.error(label, f"{key} is not allowed here; allowed: {', '.join(allowed)}")
            elif key == "$filter":
                self.check_filter(value, label, et, prefix)
            elif key == "$select":
                self.check_select(value, label, et, prefix, expanded)
            elif key == "$orderby":
                self.check_orderby(value, label, et, prefix)
            elif key in ("$top", "$skip"):
                if not value.isdigit():
                    self.error(label, f"must be a non-negative integer, got {value!r}")
            elif key == "$count":
                if value not in ("true", "false"):
                    self.error(label, "must be true or false")
            elif key == "$levels":
                self.check_levels(value, label)


def _restricted_properties(text: str) -> set[str]:
    """Properties a filter restricts the way SAP Gateway turns it into select options: conditions joined by
    top-level `and`, not negated, and an `or` only when all its branches test the same single property."""
    try:
        node = ex.parse(text)
    except ex.ParseError:
        return set()

    def conjuncts(n: ex.Node) -> list[ex.Node]:
        if isinstance(n, ex.Binary) and n.op == "and":
            return conjuncts(n.left) + conjuncts(n.right)
        return [n]

    def paths(n: ex.Node) -> set[str]:
        return {p.text for p in ex.walk(n) if isinstance(p, ex.Path)}

    def disjuncts(n: ex.Node) -> list[ex.Node]:
        if isinstance(n, ex.Binary) and n.op == "or":
            return disjuncts(n.left) + disjuncts(n.right)
        return [n]

    out: set[str] = set()
    for term in conjuncts(node):
        if isinstance(term, ex.Unary) and term.op == "not":
            continue
        if isinstance(term, ex.Binary) and term.op == "or":
            sets = [paths(d) for d in disjuncts(term)]
            if all(len(s) == 1 for s in sets) and len(set().union(*sets)) == 1:
                out |= sets[0]
            continue
        out |= paths(term)
    return out


def _is_type_name(node: ex.Node) -> bool:
    return isinstance(node, ex.Path) and len(node.segments) == 1 and "." in node.segments[0]


def _uses_variable(node: ex.Node, var: str) -> bool:
    return any(isinstance(n, ex.Path) and n.segments[0] == var for n in ex.walk(node))


def _excerpt(text: str, pos: int) -> str:
    lo = max(0, pos - 20)
    return repr(("…" if lo else "") + text[lo : pos + 20])


# ------------------------------------------------------------------------------------------------ normalisation


def normalize_options(options: Mapping[str, Any], version: Version) -> tuple[dict[str, Any], list[Issue]]:
    out: dict[str, Any] = {}
    issues: list[Issue] = []

    def fixed(option: str, message: str) -> None:
        issues.append(Issue(option=option, message=message, severity="fixed"))

    for raw_key, value in options.items():
        key = str(raw_key).strip()
        name = key.lstrip("$").lower()
        if value is None or (isinstance(value, str) and not value.strip()) or value == []:
            fixed(key, "dropped empty option")
            continue
        if name == "format":
            fixed(
                key, "dropped: the caller picks the response format (SAP V2: $format=json or Accept: application/json)"
            )
            continue
        if name not in _KNOWN:
            hint = " (aggregation is not supported; filter and select instead)" if name == "apply" else ""
            issues.append(
                Issue(
                    option=key, message=f"unsupported query option{hint}; allowed: " + ", ".join(QUERY_OPTIONS[version])
                )
            )
            continue
        target = "$" + name
        if name == "search":
            target = "search" if version == "v2" else "$search"
        elif name == "count" and version == "v2":
            if _truthy(value):
                out["$inlinecount"] = "allpages"
                fixed(key, "V2 counts with $inlinecount=allpages")
            continue
        elif name == "inlinecount" and version == "v4":
            if _truthy(value) or str(value).lower() == "allpages":
                out["$count"] = True
                fixed(key, "V4 counts with $count=true")
            continue
        if key != target:
            fixed(key, f"renamed to {target}")
        if isinstance(value, list | tuple) and target in ("$select", "$orderby", "$expand"):
            value = ",".join(str(v).strip() for v in value)
            fixed(target, "joined the list with commas")
        if isinstance(value, str) and target in ("$select", "$orderby", "$expand"):
            tidy = ",".join(ex.split_top_level(value.strip(), ","))
            if tidy != value.strip():
                fixed(target, "removed spaces around commas (some servers reject them)")
            value = tidy
        if target in ("$top", "$skip"):
            v = _to_int(value)
            if v is None or v < 0:
                issues.append(Issue(option=target, message=f"must be a non-negative integer, got {value!r}"))
                continue
            if v != value:
                fixed(target, f"converted {value!r} to {v}")
            value = v
        elif target == "$count":
            if isinstance(value, str) and value.strip().lower() in ("true", "false"):
                value = value.strip().lower() == "true"
            elif isinstance(value, int) and not isinstance(value, bool) and value in (0, 1):
                fixed(target, f"converted {value} to {'true' if value else 'false'}")
                value = bool(value)
            if not isinstance(value, bool):
                issues.append(Issue(option=target, message=f"must be true or false, got {value!r}"))
                continue
            if not value:
                continue
        elif target == "$inlinecount":
            if value is True or str(value).strip().lower() in ("allpages", "true"):
                value = "allpages"
            elif value is False or str(value).strip().lower() in ("none", "false"):
                continue
            else:
                issues.append(Issue(option=target, message=f"must be allpages or none, got {value!r}"))
                continue
        elif not isinstance(value, str):
            issues.append(Issue(option=target, message=f"must be a string, got {type(value).__name__}"))
            continue
        else:
            value = value.strip()
        out[target] = value
    ordered = {k: out[k] for k in _OPTION_ORDER if k in out}
    return ordered, issues


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v == 1
    return isinstance(v, str) and v.strip().lower() in ("true", "allpages", "1")


def _to_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


# ------------------------------------------------------------------------------------------------ entry point


def validate(
    service: Service, entity_set: str | None, query_options: Mapping[str, Any] | None, version: Version
) -> Validation:
    options, issues = normalize_options(query_options or {}, version)
    names = [s.name for s in service.entity_sets]
    if entity_set and entity_set.strip() != entity_set.strip().strip("/"):
        entity_set = entity_set.strip().strip("/")
        issues.append(Issue(option="entity_set", message="removed slashes around the name", severity="fixed"))
    es = service.entity_set(entity_set) if entity_set else None
    if es is None:
        msg = "missing" if not entity_set else f"unknown entity set {entity_set!r}{suggest(entity_set, names)}"
        issues.append(Issue(option="entity_set", message=f"{msg}; available: {', '.join(names)}"))
        return Validation(entity_set=entity_set, query_options=options, issues=issues)
    c = _Checker(service, es, version)
    c.issues = issues
    if not es.addressable:
        c.error(
            "entity_set",
            f"{es.name} cannot be queried directly (sap:addressable=false); reach it through a navigation property",
        )
    et = es.entity_type
    expanded: set[str] = set()
    if "$expand" in options:
        expanded = c.check_expand(options["$expand"], "$expand", et, "")
    if "$filter" in options:
        c.check_filter(options["$filter"], "$filter", et, "")
        if es.required_in_filter:
            restricted = _restricted_properties(options["$filter"])
            for req in es.required_in_filter:
                if req not in restricted:
                    c.error(
                        "$filter",
                        f"this service requires a filter on {req} as a top-level 'and' condition "
                        "(not under 'not', and in an 'or' only together with itself)",
                    )
    elif es.requires_filter or es.required_in_filter:
        c.error(
            "$filter",
            f"{es.name} requires a $filter"
            + (f" on {', '.join(es.required_in_filter)}" if es.required_in_filter else ""),
        )
    if "$select" in options:
        c.check_select(options["$select"], "$select", et, "", expanded)
    if "$orderby" in options:
        c.check_orderby(options["$orderby"], "$orderby", et, "")
    if ("$top" in options or "$skip" in options) and not es.pageable:
        c.error("$top" if "$top" in options else "$skip", f"{es.name} does not support paging ($top/$skip)")
    if ("$count" in options or "$inlinecount" in options) and not es.countable:
        c.error("$count", f"{es.name} does not support counting")
    for key in ("$search", "search"):
        if key not in options:
            continue
        if key == "search" and es.searchable is not True:
            c.error("search", f"{es.name} is not marked searchable (sap:searchable); use $filter with substringof")
        elif key == "$search" and es.searchable is False:
            c.error("$search", f"{es.name} does not support $search; use $filter with contains")
        elif options[key].count('"') % 2:
            c.error(key, "unbalanced double quote")
    return Validation(entity_set=es.name, query_options=options, issues=c.issues, fields=list(c.fields))
