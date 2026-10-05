"""Prompt construction, candidate generation and the repair prompt.

The system prompt carries the rules and the grammar cheat sheet of the target version (versions.CHEAT_SHEET).
The user prompt lists only the selected entity sets with their pruned properties, so wide SAP entities do not
flood the context. The LLM answers {entity_set, query_options, residual_condition, rationale}.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

from .llm_client import ChatRequest, ChatResult, LLMClient
from .metadata import EntitySet, NavigationProperty, Property, Service
from .settings import Settings
from .stats import StatsBook
from .validator import Issue
from .versions import CHEAT_SHEET, PROTOCOL, QUERY_OPTIONS, Version

CANDIDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_set": {"type": "string"},
        "query_options": {"type": "object"},
        "residual_condition": {"type": ["string", "null"]},
        "rationale": {"type": "string"},
    },
    "required": ["entity_set", "query_options", "residual_condition", "rationale"],
}

SYSTEM_TEMPLATE = """\
You translate a question into one OData {label} query against the service the user describes.

Reply with one JSON object and nothing else:
{{"entity_set": "<one of the listed entity sets>",
 "query_options": {{"<option>": <value>}},
 "residual_condition": null,
 "rationale": "<one or two sentences: which entity set and options answer the question, and why>"}}

Rules
- Use only the entity sets, properties and navigation properties the user lists. Names are case-sensitive.
  Never invent or guess a name. If the listed metadata cannot express part of the question, say so in the rationale.
- query_options keys: {keys}. Include only what the question needs. $top and $skip are JSON integers{count_rule}.
- A property that lists values only takes those codes: compare with the code, not with its meaning.
- Today is {today}. Turn relative dates ("last month", "this year", "since Monday") into literals.
- Put everything OData can express exactly into query_options: comparisons, codes, dates, text matching,
  sorting, limits.
- residual_condition: when part of the question needs a judgment about meaning that OData cannot express (tone,
  sentiment, intent: "sounds angry", "looks suspicious", "is about a refund"), write that part as a condition on
  ONE returned record, for example "the customer comment sounds angry". $select the properties needed to judge
  it, and do not cut the result with $top for that part, because records are judged after the query runs.
  Otherwise residual_condition is null.
- Select the properties the question asks about plus the keys instead of returning every property.

{cheat_sheet}
"""


@dataclass
class NavContext:
    nav: NavigationProperty
    target: str  # target entity set name, or the entity type's short name
    properties: list[Property] | None  # None: target properties not shown
    omitted: int = 0


@dataclass
class SetContext:
    entity_set: EntitySet
    properties: list[Property]
    omitted: int = 0
    navigations: list[NavContext] = field(default_factory=list)
    probability: float | None = None


@dataclass
class Candidate:
    index: int
    entity_set: str | None
    query_options: dict[str, Any]
    residual_condition: str | None
    rationale: str
    issues: list[Issue]  # parse problems (the validator adds its own)
    content: str  # raw reply, kept for the repair conversation
    repaired: bool = False


# ------------------------------------------------------------------------------------------------ prompts


def system_prompt(version: Version, today: dt.date) -> str:
    count_rule = "; $count is true or false" if version == "v4" else '; $inlinecount is "allpages"; search is a string'
    label = "V4" if version == "v4" else "V2 (SAP Gateway)"
    return SYSTEM_TEMPLATE.format(
        label=label,
        keys=", ".join(QUERY_OPTIONS[version]),
        count_rule=count_rule,
        today=today.isoformat(),
        cheat_sheet=CHEAT_SHEET[version],
    )


def describe_property(
    p: Property, *, key: bool = False, filterable: bool = True, sortable: bool = True, service: Service | None = None
) -> str:
    t = p.type
    if p.kind == "enum":
        t = f"enum {p.type}"
    if p.collection:
        t = f"Collection({t})"
    extras = []
    if p.max_length:
        extras.append(f"max {p.max_length}")
    if p.type == "Edm.DateTime" and (p.display_format or "").lower() == "date":
        extras.append("date only")
    if key:
        extras.append("key")
    head = f"- {p.name} ({', '.join([t, *extras])})"
    text = []
    if p.label and p.label.lower().replace(" ", "") != p.name.lower():
        text.append(p.label)
    if p.description and p.description != p.label:
        text.append(p.description)
    if p.values:
        vals = list(p.values.items())
        shown = ", ".join(f"'{k}'" + (f" = {m}" if m else "") for k, m in vals[:30])
        more = f" (+{len(vals) - 30} more)" if len(vals) > 30 else ""
        text.append(f"values: {shown}{more}")
    if p.kind == "complex" and service is not None:
        ct = service.complex_type(p.type)
        if ct is not None:
            text.append("members: " + ", ".join(f"{q.name} ({q.type})" for q in ct.properties))
    flags = []
    if not filterable:
        flags.append("not filterable")
    if not sortable:
        flags.append("not sortable")
    line = head + (": " + ". ".join(text) if text else "")
    return line + (f" [{', '.join(flags)}]" if flags else "")


def describe_set(ctx: SetContext, service: Service, version: Version) -> str:
    es = ctx.entity_set
    lines = [f"## {es.name}" + (f" ({es.display_label()})" if es.display_label() else "")]
    desc = es.description or es.entity_type.description
    if desc:
        lines.append(desc)
    lines.append(f"keys: {', '.join(es.keys) or '(none)'}")
    caps = []
    if es.searchable is True:
        caps.append("free-text search supported")
    if es.searchable is False or (version == "v2" and es.searchable is not True):
        caps.append("no free-text search")
    if not es.pageable:
        caps.append("no $top/$skip")
    if not es.countable:
        caps.append("no counting")
    if es.requires_filter or es.required_in_filter:
        caps.append(
            "a $filter is required" + (f" on {', '.join(es.required_in_filter)}" if es.required_in_filter else "")
        )
    if caps:
        lines.append("capabilities: " + "; ".join(caps))
    lines.append("properties:")
    for p in ctx.properties:
        lines.append(
            describe_property(
                p,
                key=p.name in es.keys,
                filterable=p.filterable and p.name not in es.non_filterable,
                sortable=p.sortable and p.name not in es.non_sortable,
                service=service,
            )
        )
    if ctx.omitted:
        lines.append(f"({ctx.omitted} more properties not shown because they look irrelevant to the question)")
    if ctx.navigations:
        lines.append("navigation properties:")
        for n in ctx.navigations:
            card = "collection" if n.nav.collection else "single"
            head = f"- {n.nav.name} -> {n.target} ({card})"
            if n.nav.label:
                head += f": {n.nav.label}"
            if n.properties is None:
                lines.append(head + " [target properties not shown]")
                continue
            lines.append(head)
            for p in n.properties:
                lines.append("  " + describe_property(p, filterable=p.filterable, sortable=p.sortable, service=service))
            if n.omitted:
                lines.append(f"  ({n.omitted} more properties not shown)")
    return "\n".join(lines)


def user_prompt(question: str, contexts: list[SetContext], service: Service, version: Version) -> str:
    root = service.service_url or "(service root not given)"
    parts = [f"Service: {root} (OData {PROTOCOL[version]})", ""]
    if len(contexts) > 1:
        parts.append("More than one entity set might answer the question; pick the best one.\n")
    parts += [describe_set(c, service, version) + "\n" for c in contexts]
    parts.append(f"Question: {question}")
    return "\n".join(parts)


def build_messages(
    question: str, contexts: list[SetContext], service: Service, version: Version, today: dt.date
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system_prompt(version, today)},
        {"role": "user", "content": user_prompt(question, contexts, service, version)},
    ]


def repair_messages(messages: list[dict[str, str]], candidate: Candidate, issues: list[Issue]) -> list[dict[str, str]]:
    problems = "\n".join(f"- {i}" for i in issues if i.severity == "error")
    return [
        *messages,
        {"role": "assistant", "content": candidate.content},
        {
            "role": "user",
            "content": (
                "That answer failed validation against the service metadata:\n"
                f"{problems}\n"
                "Return the corrected JSON object with the same four fields. Use only names listed above and the "
                "syntax of the cheat sheet. If a property you wanted does not exist, answer with the properties that "
                "do exist and mention the gap in the rationale."
            ),
        },
    ]


# ------------------------------------------------------------------------------------------------ parsing


def _options_from_string(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for part in text.lstrip("?").split("&"):
        if not part:
            continue
        k, _, v = part.partition("=")
        out[unquote(k)] = unquote(v)
    return out


def parse_candidate(index: int, result: ChatResult | Exception) -> Candidate:
    if isinstance(result, Exception):
        return Candidate(index, None, {}, None, "", [Issue(option="llm", message=str(result))], "")
    if result.data is None:
        issue = Issue(option="json", message=f"the reply was not a JSON object ({result.error})")
        return Candidate(index, None, {}, None, "", [issue], result.content)
    d = result.data
    issues: list[Issue] = []
    es = d.get("entity_set") or d.get("entitySet") or d.get("entity")
    if es is not None and not isinstance(es, str):
        issues.append(Issue(option="entity_set", message="must be a string"))
        es = None
    opts = d.get("query_options", d.get("options", {}))
    if isinstance(opts, str):
        opts = _options_from_string(opts)
        issues.append(Issue(option="query_options", message="parsed a query string into options", severity="fixed"))
    if not isinstance(opts, dict):
        issues.append(Issue(option="query_options", message="must be a JSON object"))
        opts = {}
    residual = d.get("residual_condition")
    if not isinstance(residual, str) or not residual.strip() or residual.strip().lower() in ("null", "none"):
        residual = None
    rationale = d.get("rationale")
    rationale = rationale.strip() if isinstance(rationale, str) else ""
    return Candidate(index, es, opts, residual.strip() if residual else None, rationale, issues, result.content)


# ------------------------------------------------------------------------------------------------ generation


def generate(
    llm: LLMClient,
    messages: list[dict[str, str]],
    settings: Settings,
    n: int,
    call_stats: StatsBook | None = None,
) -> list[Candidate]:
    """N independent chats at LLM_TEMPERATURE with seeds LLM_SEED + i, run concurrently."""
    base = settings.llm_seed
    reqs = [
        ChatRequest(messages, settings.llm_temperature, None if base is None else base + i, "generate")
        for i in range(n)
    ]
    results = llm.chat_json_many(reqs, schema=CANDIDATE_SCHEMA, call_stats=call_stats)
    return [parse_candidate(i, r) for i, r in enumerate(results)]


def repair(
    llm: LLMClient,
    messages: list[dict[str, str]],
    broken: list[tuple[Candidate, list[Issue]]],
    settings: Settings,
    call_stats: StatsBook | None = None,
) -> list[Candidate]:
    """One repair call per invalid candidate, at temperature 0, all concurrently."""
    base = settings.llm_seed
    reqs = [
        ChatRequest(repair_messages(messages, c, issues), 0.0, None if base is None else base + c.index, "repair")
        for c, issues in broken
    ]
    results = llm.chat_json_many(reqs, schema=CANDIDATE_SCHEMA, call_stats=call_stats)
    out = []
    for (c, _), r in zip(broken, results, strict=True):
        fixed = parse_candidate(c.index, r)
        fixed.repaired = True
        out.append(fixed)
    return out
