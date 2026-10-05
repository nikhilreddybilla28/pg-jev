"""Request, token and cost counters, kept per stage label ("select", "prune", "verify", "generate", ...).

The Jev and LLM clients keep one `StatsBook` for their whole session (like pg-jev's `jev_stats()`) and also record
into a per-call book that `text_to_odata` returns in `Result.stats`.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, fields
from typing import Any

JEV_USD_PER_INPUT_TOKEN = 0.042 / 1_000_000  # jev-1.13 list price; output tokens are free (same as pg-jev)


@dataclass
class Counters:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    items: int = 0
    cache_hits: int = 0
    errors: int = 0
    retries: int = 0
    api_ms: float = 0.0
    cost_usd: float | None = 0.0  # None: no price configured, cost unknown

    def add(self, other: Counters) -> None:
        for f in fields(self):
            a, b = getattr(self, f.name), getattr(other, f.name)
            if f.name == "cost_usd":
                setattr(self, f.name, None if a is None or b is None else a + b)
            else:
                setattr(self, f.name, a + b)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        cost = d.pop("cost_usd")
        d["api_ms"] = round(d["api_ms"], 1)
        d["estimated_cost_usd"] = None if cost is None else round(cost, 8)
        return d


class StatsBook:
    """Thread-safe counters per label. Worker threads record, the caller reads."""

    def __init__(self, priced: bool = True) -> None:
        self._lock = threading.Lock()
        self._priced = priced
        self._by_label: dict[str, Counters] = {}

    def record(self, label: str, **delta: Any) -> None:
        with self._lock:
            c = self._by_label.get(label)
            if c is None:
                c = self._by_label[label] = Counters(cost_usd=0.0 if self._priced else None)
            for k, v in delta.items():
                if k == "cost_usd":
                    if c.cost_usd is not None and v is not None:
                        c.cost_usd += v
                else:
                    setattr(c, k, getattr(c, k) + v)

    def total(self) -> Counters:
        with self._lock:
            t = Counters(cost_usd=0.0 if self._priced else None)
            for c in self._by_label.values():
                t.add(c)
            return t

    def labels(self) -> dict[str, Counters]:
        with self._lock:
            return {k: Counters(**asdict(v)) for k, v in self._by_label.items()}

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {k: v.as_dict() for k, v in self.labels().items()}
        d["total"] = self.total().as_dict()
        return d


def record_all(books: list[StatsBook | None], label: str, **delta: Any) -> None:
    for b in books:
        if b is not None:
            b.record(label, **delta)
