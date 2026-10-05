"""text_to_odata: select entity sets (Jev) → prune properties (Jev) → generate candidates (LLM) → validate and
repair → verify and rerank (Jev). Plus judge_rows / filter_rows for residual conditions OData cannot express.

A `Session` holds one Jev client and one LLM client: thread pools, keep-alive connections, the Jev answer cache
and cumulative stats, the equivalent of pg-jev's per-backend `GD["jev"]`. The module-level functions use a
default session built from environment variables.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from .builder import BuiltQuery, build_query
from .errors import MetadataError, NoValidQueryError
from .generator import Candidate, NavContext, SetContext, build_messages, generate, repair
from .jev_client import JevClient
from .llm_client import LLMClient
from .metadata import EntitySet, NavigationProperty, Property, Service, load_tools
from .settings import Settings, normalize_version
from .stats import StatsBook
from .validator import Issue, Validation, validate
from .versions import PROTOCOL, Version

log = logging.getLogger("odata_jev")

SELECT_Q = "Can the entity set `{ref}` answer the question stated in `question`?"
FIELD_Q = "Is `{ref}` needed to answer the question stated in `question`?"
VERIFY_Q = "Does the OData query `{ref}` correctly answer the question stated in `question`?"
ROW_Q = "Does the record `{ref}` satisfy the condition stated in `condition`?"  # pg-jev's question, verbatim


# ------------------------------------------------------------------------------------------------ results


class EntitySetScore(BaseModel):
    name: str
    probability: float | None  # None: only one entity set, Jev was not asked
    selected: bool


class CandidateReport(BaseModel):
    index: int
    entity_set: str | None
    query_options: dict[str, Any]
    residual_condition: str | None
    rationale: str
    repaired: bool
    valid: bool
    issues: list[Issue]
    query: str | None = None  # readable EntitySet?$filter=... for valid candidates
    probability: float | None = None  # Jev's verification probability
    votes: int = 0  # how many candidates produced the same query


class Result(BaseModel):
    question: str
    version: Literal["v2", "v4"]
    entity_set: str
    query: str = Field(description="readable form without percent-encoding: EntitySet?$filter=...&$top=5")
    query_url: str
    query_path: str
    query_string: str
    query_options: dict[str, Any]
    parts: dict[str, str]
    confidence: float = Field(description="Jev's probability that the chosen query answers the question")
    explanation: str
    residual_condition: str | None
    entity_sets: list[EntitySetScore]
    candidates: list[CandidateReport]
    warnings: list[str]
    stats: dict[str, Any]


class RowJudgment(BaseModel):
    row: Any
    probability: float
    passed: bool


# ------------------------------------------------------------------------------------------------ Jev items


def _short(type_name: str) -> str:
    return type_name.rpartition(".")[2]


def _target_name(service: Service, nav: NavigationProperty, source: EntitySet) -> str:
    ts = service.nav_target_set(nav, source)
    return ts.name if ts else _short(nav.target_type)


def set_summary(es: EntitySet, service: Service, max_props: int) -> dict[str, Any]:
    d: dict[str, Any] = {"name": es.name}
    if es.display_label():
        d["label"] = es.display_label()
    desc = es.description or es.entity_type.description
    if desc:
        d["description"] = desc
    d["keys"] = es.keys
    props = [
        p.name if not p.label or p.label.lower().replace(" ", "") == p.name.lower() else f"{p.name} ({p.label})"
        for p in es.properties
    ]
    d["properties"] = props[:max_props]
    if len(props) > max_props:
        d["more_properties"] = len(props) - max_props
    navs = [
        f"{n.name} -> {_target_name(service, n, es)}" + (" (many)" if n.collection else "")
        for n in es.navigation_properties
    ]
    if navs:
        d["navigation"] = navs
    return d


def field_item(owner: str, p: Property, via: str | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"entity_set": owner}
    if via:
        d["via"] = via
    d["property"] = p.name
    d["type"] = p.type
    if p.label:
        d["label"] = p.label
    if p.description:
        d["description"] = p.description
    if p.values:
        d["values"] = [f"{k} = {m}" if m else k for k, m in list(p.values.items())[:10]]
    return d


def nav_item(es: EntitySet, n: NavigationProperty, service: Service) -> dict[str, Any]:
    d: dict[str, Any] = {
        "entity_set": es.name,
        "navigation_property": n.name,
        "target": _target_name(service, n, es),
        "collection": n.collection,
    }
    target_set = service.nav_target_set(n, es)
    target_type = service.nav_target(n)
    label = (
        n.label or (target_set.display_label() if target_set else None) or (target_type.label if target_type else None)
    )
    if label:
        d["label"] = label
    desc = (target_set.description if target_set else None) or (target_type.description if target_type else None)
    if desc:
        d["description"] = desc
    return d


def verify_item(
    service: Service, es: EntitySet, v: Validation, built: BuiltQuery, residual: str | None
) -> dict[str, Any]:
    d: dict[str, Any] = {"entity_set": es.name}
    if es.display_label():
        d["entity_set_label"] = es.display_label()
    d["query"] = built.readable
    fields: dict[str, str] = {}
    for path in v.fields[:25]:
        r = service.resolve(es.entity_type, path)
        if isinstance(r, Property):
            text = r.label or ""
            if r.values:
                codes = ", ".join(f"{k} = {m}" if m else k for k, m in list(r.values.items())[:10])
                text = f"{text} ({codes})" if text else codes
            if text:
                fields[path] = text
        elif isinstance(r, NavigationProperty):
            fields[path] = r.label or f"navigation to {_short(r.target_type)}"
    if fields:
        d["fields"] = fields
    if residual:
        d["residual_condition"] = residual
        d["note"] = "the caller filters the returned records by residual_condition"
    return d


def _pick(
    scored: list[tuple[Property, float]], keys: Sequence[str], threshold: float, min_n: int, max_n: int
) -> list[Property]:
    """Keys always; then everything at or above the threshold; at least min_n and at most max_n by probability.
    Output keeps the declaration order, which is how the service lists them."""
    order = {p.name: i for i, (p, _) in enumerate(scored)}
    by_p = sorted(scored, key=lambda t: (-t[1], order[t[0].name]))
    chosen = {p.name: p for p, _ in scored if p.name in keys}
    for p, prob in by_p:
        if len(chosen) >= max_n:
            break
        if p.name not in chosen and (prob >= threshold or len(chosen) < min_n):
            chosen[p.name] = p
    return sorted(chosen.values(), key=lambda p: order[p.name])


# ------------------------------------------------------------------------------------------------ session


class Session:
    def __init__(
        self, settings: Settings | None = None, *, jev: JevClient | None = None, llm: LLMClient | None = None
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.jev = jev or JevClient(self.settings)
        self.llm = llm or LLMClient(self.settings)

    def close(self) -> None:
        self.jev.close()
        self.llm.close()

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def stats(self) -> dict[str, Any]:
        """Cumulative counters for this session, like pg-jev's jev_stats()."""
        j = self.jev.stats.as_dict()
        j["cached_answers"] = self.jev.cached_answers
        return {"jev": j, "llm": self.llm.stats.as_dict()}

    # -------------------------------------------------------------------------------- text → OData
    def text_to_odata(
        self,
        question: str,
        tools: Service | Mapping[str, Any] | str | bytes | os.PathLike[str],
        version: str | None = None,
        n_candidates: int | None = None,
        *,
        today: dt.date | str | None = None,
        service_url: str | None = None,
    ) -> Result:
        t0 = time.monotonic()
        s = self.settings
        if not question or not question.strip():
            raise ValueError("question is empty")
        s.require_llm()  # fail before spending anything on Jev
        s.require_jev_key()
        service = load_tools(tools, service_url=service_url)
        warnings: list[str] = []
        ver = self._version(version, service, warnings)
        day = dt.date.fromisoformat(today) if isinstance(today, str) else (today or dt.date.today())
        n = n_candidates or s.n_candidates
        jev_book, llm_book = StatsBook(priced=True), StatsBook(priced=self.llm.priced)

        scores, selected = self._select(question, service, jev_book, warnings)
        contexts = self._prune(question, service, selected, scores, jev_book)
        messages = build_messages(question, contexts, service, ver, day)

        candidates = generate(self.llm, messages, s, n, llm_book)
        checked = [(c, self._check(c, service, ver, selected)) for c in candidates]
        broken = [(c, v) for c, v in checked if not v[1] and not any(i.option == "llm" for i in c.issues)]
        if s.repair and broken:
            fixed = repair(self.llm, messages, [(c, v[0]) for c, v in broken], s, llm_book)
            repaired = {c.index: (c, self._check(c, service, ver, selected)) for c in fixed}
            checked = [repaired.get(c.index, (c, v)) for c, v in checked]

        reports: list[CandidateReport] = []
        distinct: dict[str, tuple[Candidate, Validation, BuiltQuery, list[int]]] = {}
        for c, (issues, ok, validation) in checked:
            rep = CandidateReport(
                index=c.index,
                entity_set=c.entity_set,
                query_options=validation.query_options if validation else c.query_options,
                residual_condition=c.residual_condition,
                rationale=c.rationale,
                repaired=c.repaired,
                valid=ok,
                issues=issues,
            )
            reports.append(rep)
            if not ok or validation is None:
                continue
            built = build_query(validation.entity_set or "", validation.query_options, service.service_url)
            key = built.path + "\x00" + (c.residual_condition or "")
            rep.query = built.readable
            if key in distinct:
                distinct[key][3].append(c.index)
            else:
                distinct[key] = (c, validation, built, [c.index])

        if not distinct:
            stats = self._call_stats(jev_book, llm_book, t0)
            raise NoValidQueryError(
                f"odata-jev: none of the {n} candidates passed validation"
                + (" after the repair round" if s.repair else ""),
                reports,
                stats,
            )

        groups = list(distinct.values())
        items = [
            verify_item(service, service.entity_set(v.entity_set or ""), v, b, c.residual_condition)  # type: ignore[arg-type]
            for c, v, b, _ in groups
        ]
        context = {"question": question, "odata_version": PROTOCOL[ver], "today": day.isoformat()}
        probs = self.jev.noul(
            items, instruction=VERIFY_Q, context=context, items_key="candidates", label="verify", call_stats=jev_book
        )
        for (_c, _v, _b, idxs), p in zip(groups, probs, strict=True):
            for i in idxs:
                reports[i].probability = p
                reports[i].votes = len(idxs)
        best = max(range(len(groups)), key=lambda g: (probs[g], len(groups[g][3]), -groups[g][3][0]))
        chosen, validation, built, idxs = groups[best]
        confidence = probs[best]
        explanation = chosen.rationale or f"Queries {built.entity_set}."
        stats = self._call_stats(jev_book, llm_book, t0)
        log.info(
            "odata-jev: %s (confidence %.2f, %d of %d candidates agree) in %.0f ms, ≈$%.6f",
            built.readable,
            confidence,
            len(idxs),
            len(candidates),
            stats["elapsed_ms"],
            stats["estimated_cost_usd"],
        )
        return Result(
            question=question,
            version=ver,
            entity_set=built.entity_set,
            query=built.readable,
            query_url=built.url,
            query_path=built.path,
            query_string=built.query_string,
            query_options=validation.query_options,
            parts=built.parts,
            confidence=confidence,
            explanation=explanation,
            residual_condition=chosen.residual_condition,
            entity_sets=scores,
            candidates=reports,
            warnings=warnings,
            stats=stats,
        )

    # -------------------------------------------------------------------------------- stages
    def _version(self, explicit: str | None, service: Service, warnings: list[str]) -> Version:
        if explicit:
            v: Version = normalize_version(explicit)
            if service.version and service.version != v:
                warnings.append(f"the metadata is OData {service.version.upper()} but the query is OData {v.upper()}")
            return v
        return service.version or self.settings.odata_version

    def _select(
        self, question: str, service: Service, book: StatsBook, warnings: list[str]
    ) -> tuple[list[EntitySetScore], list[EntitySet]]:
        sets = [es for es in service.entity_sets if es.addressable]
        if not sets:
            raise MetadataError("the service has no entity set that can be queried directly")
        if len(sets) == 1:
            return [EntitySetScore(name=sets[0].name, probability=None, selected=True)], sets
        s = self.settings
        items = [set_summary(es, service, s.selection_max_properties) for es in sets]
        probs = self.jev.noul(
            items,
            instruction=SELECT_Q,
            context={"question": question},
            items_key="entity_sets",
            label="select",
            call_stats=book,
        )
        ranked = sorted(zip(sets, probs, strict=True), key=lambda t: -t[1])
        selected = [ranked[0][0]]
        if ranked[0][1] - ranked[1][1] < s.selection_margin:
            selected.append(ranked[1][0])
        if ranked[0][1] < s.selection_min_probability:
            warnings.append(
                f"no entity set clearly answers the question (best: {ranked[0][0].name} at p={ranked[0][1]:.2f})"
            )
        scores = [EntitySetScore(name=es.name, probability=p, selected=es in selected) for es, p in ranked]
        return scores, selected

    def _prune(
        self, question: str, service: Service, selected: list[EntitySet], scores: list[EntitySetScore], book: StatsBook
    ) -> list[SetContext]:
        s = self.settings
        ctx = {"question": question}
        # stage A: the set's own properties and its navigation properties, for sets wider than prune_above
        items_a: list[dict[str, Any]] = []
        owners_a: list[tuple[int, Property | NavigationProperty]] = []
        for si, es in enumerate(selected):
            if len(es.properties) + len(es.navigation_properties) <= s.prune_above:
                continue
            for p in es.properties:
                items_a.append(field_item(es.name, p))
                owners_a.append((si, p))
            for n in es.navigation_properties:
                items_a.append(nav_item(es, n, service))
                owners_a.append((si, n))
        probs_a = (
            self.jev.noul(items_a, instruction=FIELD_Q, context=ctx, items_key="fields", label="prune", call_stats=book)
            if items_a
            else []
        )
        p_of: dict[tuple[int, str, str], float] = {}
        for (si, obj), prob in zip(owners_a, probs_a, strict=True):
            p_of[(si, "nav" if isinstance(obj, NavigationProperty) else "prop", obj.name)] = prob

        contexts: list[SetContext] = []
        kept_navs: list[tuple[int, NavigationProperty]] = []
        for si, es in enumerate(selected):
            pruned = any(k[0] == si for k in p_of)
            if pruned:
                scored = [(p, p_of[(si, "prop", p.name)]) for p in es.properties]
                props = _pick(scored, es.keys, s.property_threshold, s.min_properties, s.max_properties)
                navs = [n for n in es.navigation_properties if p_of[(si, "nav", n.name)] >= s.property_threshold]
            else:
                props, navs = list(es.properties), list(es.navigation_properties)
            score = next((x.probability for x in scores if x.name == es.name), None)
            contexts.append(SetContext(es, props, len(es.properties) - len(props), [], score))
            kept_navs += [(si, n) for n in navs]

        # stage B: properties of the navigation targets kept in stage A
        items_b: list[dict[str, Any]] = []
        owners_b: list[tuple[int, NavigationProperty, Property]] = []
        for si, n in kept_navs:
            target = service.nav_target(n)
            if target is None or len(target.properties) <= s.prune_above:
                continue
            for p in target.properties:
                items_b.append(
                    field_item(_target_name(service, n, selected[si]), p, via=f"{selected[si].name}/{n.name}")
                )
                owners_b.append((si, n, p))
        probs_b = (
            self.jev.noul(items_b, instruction=FIELD_Q, context=ctx, items_key="fields", label="prune", call_stats=book)
            if items_b
            else []
        )
        pb: dict[tuple[int, str, str], float] = {
            (si, n.name, p.name): prob for (si, n, p), prob in zip(owners_b, probs_b, strict=True)
        }
        kept = {(si, n.name) for si, n in kept_navs}
        for si, es in enumerate(selected):
            for n in es.navigation_properties:
                target = service.nav_target(n)
                name = _target_name(service, n, es)
                if (si, n.name) not in kept or target is None:
                    contexts[si].navigations.append(NavContext(n, name, None))
                    continue
                if len(target.properties) <= s.prune_above:
                    contexts[si].navigations.append(NavContext(n, name, list(target.properties)))
                    continue
                scored = [(p, pb[(si, n.name, p.name)]) for p in target.properties]
                tprops = _pick(
                    scored, target.keys, s.property_threshold, max(3, s.min_properties // 2), s.max_properties
                )
                contexts[si].navigations.append(NavContext(n, name, tprops, len(target.properties) - len(tprops)))
        return contexts

    def _check(
        self, c: Candidate, service: Service, version: Version, selected: list[EntitySet]
    ) -> tuple[list[Issue], bool, Validation | None]:
        if c.entity_set is None and not c.query_options and any(i.severity == "error" for i in c.issues):
            return list(c.issues), False, None
        v = validate(service, c.entity_set, c.query_options, version)
        issues = list(c.issues) + list(v.issues)
        if v.valid and v.entity_set not in {es.name for es in selected}:
            issues.append(
                Issue(
                    option="entity_set",
                    severity="warning",
                    message=f"{v.entity_set} was not among the entity sets Jev selected",
                )
            )
        ok = not any(i.severity == "error" for i in issues)
        return issues, ok, v

    @staticmethod
    def _call_stats(jev_book: StatsBook, llm_book: StatsBook, t0: float) -> dict[str, Any]:
        j, m = jev_book.as_dict(), llm_book.as_dict()
        jc = j["total"]["estimated_cost_usd"] or 0.0
        lc = m["total"]["estimated_cost_usd"]
        return {
            "jev": j,
            "llm": m,
            "estimated_cost_usd": round(jc + (lc or 0.0), 8),
            "llm_cost_known": lc is not None,
            "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
        }

    # -------------------------------------------------------------------------------- residual conditions
    def judge_rows(
        self,
        rows: Iterable[Mapping[str, Any]],
        condition: str,
        *,
        threshold: float = 0.5,
        fields: Sequence[str] | None = None,
    ) -> list[RowJudgment]:
        """Ask Jev whether each record satisfies `condition`, batched exactly like pg-jev's jev(): 20 rows per
        request in one shared state {"condition", "rows"}. OData bookkeeping (__metadata, @odata.*, deferred
        navigation stubs) is stripped before sending; `fields` limits which properties leave your system."""
        rows = list(rows)
        payload = [clean_record(r, fields) for r in rows]
        probs = self.jev.noul(
            payload, instruction=ROW_Q, context={"condition": condition}, items_key="rows", label="judge"
        )
        return [RowJudgment(row=r, probability=p, passed=p >= threshold) for r, p in zip(rows, probs, strict=True)]

    def filter_rows(
        self,
        rows: Iterable[Mapping[str, Any]],
        condition: str,
        *,
        threshold: float = 0.5,
        fields: Sequence[str] | None = None,
    ) -> list[Mapping[str, Any]]:
        return [j.row for j in self.judge_rows(rows, condition, threshold=threshold, fields=fields) if j.passed]


# ------------------------------------------------------------------------------------------------ records


def clean_record(row: Any, fields: Sequence[str] | None = None) -> Any:
    if isinstance(row, Mapping):
        out: dict[str, Any] = {}
        for k, v in row.items():
            if k in ("__metadata", "__deferred") or k.startswith(("@", "odata.")):
                continue
            if fields is not None and k not in fields:
                continue
            if isinstance(v, Mapping) and "__deferred" in v:
                continue  # V2 navigation link that was not expanded
            out[k] = clean_record(v)
        return out
    if isinstance(row, list):
        return [clean_record(x) for x in row]
    return row


def extract_records(payload: Any) -> list[dict[str, Any]]:
    """The records of an OData JSON response: V4 {"value": [...]}, V2 {"d": {"results": [...]}}, V1 {"d": [...]},
    a single entity, or a plain list."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, Mapping):
        raise ValueError("not an OData JSON response")
    if "value" in payload and isinstance(payload["value"], list):
        return list(payload["value"])
    d = payload.get("d")
    if isinstance(d, Mapping) and isinstance(d.get("results"), list):
        return list(d["results"])
    if isinstance(d, list):
        return d
    if isinstance(d, Mapping):
        return [dict(d)]
    return [dict(payload)]


# ------------------------------------------------------------------------------------------------ module API

_default: Session | None = None
_default_lock = threading.Lock()


def default_session() -> Session:
    global _default
    with _default_lock:
        if _default is None:
            _default = Session(Settings.from_env())
        return _default


def text_to_odata(
    question: str,
    tools: Service | Mapping[str, Any] | str | bytes | os.PathLike[str],
    version: str | None = None,
    n_candidates: int | None = None,
    *,
    session: Session | None = None,
    settings: Settings | None = None,
    today: dt.date | str | None = None,
    service_url: str | None = None,
) -> Result:
    """Translate `question` into one OData query for the service described by `tools`.

    `tools` is a `Service`, JSON tool details (mapping, string or path) or a $metadata EDMX document (string,
    bytes or path). `version` ("v2"/"v4") defaults to the version the metadata declares, then ODATA_VERSION.
    """
    if session is not None:
        return session.text_to_odata(question, tools, version, n_candidates, today=today, service_url=service_url)
    if settings is not None:
        with Session(settings) as s:
            return s.text_to_odata(question, tools, version, n_candidates, today=today, service_url=service_url)
    return default_session().text_to_odata(question, tools, version, n_candidates, today=today, service_url=service_url)


def judge_rows(
    rows: Iterable[Mapping[str, Any]],
    condition: str,
    *,
    threshold: float = 0.5,
    fields: Sequence[str] | None = None,
    session: Session | None = None,
    settings: Settings | None = None,
) -> list[RowJudgment]:
    if session is not None:
        return session.judge_rows(rows, condition, threshold=threshold, fields=fields)
    if settings is not None:
        with Session(settings) as s:
            return s.judge_rows(rows, condition, threshold=threshold, fields=fields)
    return default_session().judge_rows(rows, condition, threshold=threshold, fields=fields)


def filter_rows(
    rows: Iterable[Mapping[str, Any]],
    condition: str,
    *,
    threshold: float = 0.5,
    fields: Sequence[str] | None = None,
    session: Session | None = None,
    settings: Settings | None = None,
) -> list[Mapping[str, Any]]:
    return [
        j.row
        for j in judge_rows(rows, condition, threshold=threshold, fields=fields, session=session, settings=settings)
        if j.passed
    ]
