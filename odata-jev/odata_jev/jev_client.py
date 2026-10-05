"""Client for TypeSafe's Jev (System One) API, ported from pg-jev's `_jev_eval`.

What carries over from pg-jev:

* one request = one shared `state` plus one noul question per item, `jev_batch_size` (20) items per request,
  because Jev locates `items[i]` by position and accuracy drops in longer arrays;
* a thread pool of `jev_concurrency` workers over pooled keep-alive connections (httpx), with TCP keepalive
  probes so a silently dropped connection fails fast;
* retries on 408/429/529/5xx and transport errors, honouring `retry-after-ms` / `Retry-After`, exponential
  backoff with jitter; one immediate retry for a keep-alive connection the server closed;
* answers cached per session by content hash; waits sliced at 250 ms so Ctrl-C stays responsive;
* a spend guard per call and request/token/cost counters (`stats`), the counterpart of `jev_stats()`.

Worker threads never log at INFO or raise to the caller directly; the calling thread collects results.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import socket
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from concurrent.futures import FIRST_EXCEPTION, Future, ThreadPoolExecutor, wait
from typing import Any

import httpx

from . import __version__
from .errors import JevError, JevSpendLimitError
from .settings import Settings
from .stats import JEV_USD_PER_INPUT_TOKEN, StatsBook, record_all

log = logging.getLogger("odata_jev.jev")

MAX_ATTEMPTS = 7


def _socket_options() -> list[tuple[int, int, int]]:
    """Probe after 30 s idle, every 10 s, 3 misses: a peer dropped without FIN fails within about a minute."""
    opts = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
    for name, value in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
        if hasattr(socket, name):
            opts.append((socket.IPPROTO_TCP, getattr(socket, name), value))
    return opts


def _retry_after(resp: httpx.Response) -> float | None:
    v = resp.headers.get("retry-after-ms")
    if v and v.strip().isdigit():
        return int(v) / 1000.0
    v = resp.headers.get("retry-after")
    if v:
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


class JevClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        cache_size: int = 100_000,
    ) -> None:
        self.settings = settings
        self.stats = StatsBook(priced=True)
        self.cache_size = cache_size
        self._transport = transport
        self._cache: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()
        self._http: httpx.Client | None = None
        self._pool: ThreadPoolExecutor | None = None

    # ------------------------------------------------------------------------------------------ lifecycle
    def _client(self) -> httpx.Client:
        with self._lock:
            if self._http is None:
                s = self.settings
                limits = httpx.Limits(
                    max_connections=s.jev_concurrency,
                    max_keepalive_connections=s.jev_concurrency,
                    keepalive_expiry=s.jev_keepalive,
                )
                transport = self._transport or httpx.HTTPTransport(limits=limits, socket_options=_socket_options())
                headers = {"Content-Type": "application/json", "User-Agent": f"odata-jev/{__version__}"}
                if s.typesafe_api_key:
                    headers["Authorization"] = f"Bearer {s.typesafe_api_key}"
                self._http = httpx.Client(
                    transport=transport, limits=limits, headers=headers, timeout=httpx.Timeout(s.jev_timeout)
                )
            return self._http

    def _executor(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=self.settings.jev_concurrency, thread_name_prefix="jev")
            return self._pool

    def close(self) -> None:
        with self._lock:
            if self._pool is not None:
                self._pool.shutdown(wait=False, cancel_futures=True)
                self._pool = None
            if self._http is not None:
                self._http.close()
                self._http = None

    def __enter__(self) -> JevClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def cache_clear(self) -> None:
        with self._lock:
            self._cache.clear()

    @property
    def cached_answers(self) -> int:
        with self._lock:
            return len(self._cache)

    # ------------------------------------------------------------------------------------------ public API
    def noul(
        self,
        items: Sequence[Any],
        *,
        instruction: str,
        context: Mapping[str, Any] | None = None,
        items_key: str = "items",
        label: str = "jev",
        call_stats: StatsBook | None = None,
    ) -> list[float]:
        """Probability (0..1) that the yes/no `instruction` holds for each item.

        `instruction` contains `{ref}`, replaced by `items_key[i]`; `context` is shared by every item of a
        request, e.g. instruction "Does the record `{ref}` satisfy the condition stated in `condition`?" with
        context {"condition": "..."} and items_key "rows" is exactly pg-jev's row question.
        """
        if "{ref}" not in instruction:
            raise ValueError("instruction must contain {ref}")
        ctx = dict(context or {})
        if items_key in ctx:
            raise ValueError(f"context must not contain the items key {items_key!r}")
        if not items:
            return []
        self.settings.require_jev_key()
        books = [self.stats, call_stats]
        t0 = time.monotonic()
        model = self.settings.jev_model
        keys = [hashlib.sha1(_dumps([model, instruction, items_key, ctx, it]).encode()).hexdigest() for it in items]
        out: list[float | None] = [None] * len(items)
        pending: dict[str, list[int]] = {}
        with self._lock:
            for i, k in enumerate(keys):
                if k in self._cache:
                    out[i] = self._cache[k]
                    self._cache.move_to_end(k)
                else:
                    pending.setdefault(k, []).append(i)
        hits = len(items) - sum(len(v) for v in pending.values())
        if hits:
            record_all(books, label, cache_hits=hits)
        reqs = toks = 0
        if pending:
            unique = [(k, items[idxs[0]]) for k, idxs in pending.items()]
            self._guard(unique)
            size = self.settings.jev_batch_size
            batches = [unique[i : i + size] for i in range(0, len(unique), size)]
            pool = self._executor()
            futures = [pool.submit(self._run_batch, ctx, items_key, instruction, b, label, call_stats) for b in batches]
            for answers, tokens in self._wait(futures):
                reqs += 1
                toks += tokens
                for k, p in answers.items():
                    for i in pending[k]:
                        out[i] = p
        log.info(
            "jev[%s]: %d item%s, %d cached, %d request%s, %d input tokens (≈$%.6f), %.0f ms",
            label,
            len(items),
            "" if len(items) == 1 else "s",
            hits,
            reqs,
            "" if reqs == 1 else "s",
            toks,
            toks * JEV_USD_PER_INPUT_TOKEN,
            (time.monotonic() - t0) * 1000,
        )
        return [float(p) for p in out]  # type: ignore[arg-type]

    # ------------------------------------------------------------------------------------------ internals
    def _guard(self, unique: list[tuple[str, Any]]) -> None:
        s = self.settings
        if s.jev_max_items_per_call and len(unique) > s.jev_max_items_per_call:
            raise JevSpendLimitError(
                f"odata-jev: this call would send {len(unique)} items to Jev, above "
                f"JEV_MAX_ITEMS_PER_CALL = {s.jev_max_items_per_call}"
            )
        if s.jev_max_chars_per_call:
            chars = sum(len(_dumps(it)) for _, it in unique)
            if chars > s.jev_max_chars_per_call:
                raise JevSpendLimitError(
                    f"odata-jev: this call would send {chars} characters to Jev, above "
                    f"JEV_MAX_CHARS_PER_CALL = {s.jev_max_chars_per_call}"
                )

    def _wait(self, futures: list[Future[tuple[dict[str, float], int]]]) -> list[tuple[dict[str, float], int]]:
        """Wait in 250 ms slices (Ctrl-C stays responsive); the first failure cancels the rest and is raised."""
        try:
            remaining = set(futures)
            while remaining:
                done, remaining = wait(remaining, timeout=0.25, return_when=FIRST_EXCEPTION)
                for f in done:
                    if f.exception() is not None:
                        raise f.exception()  # type: ignore[misc]
            return [f.result() for f in futures]
        except BaseException:
            for f in futures:
                f.cancel()
            raise

    def _run_batch(
        self,
        ctx: dict[str, Any],
        items_key: str,
        instruction: str,
        batch: list[tuple[str, Any]],
        label: str,
        call_stats: StatsBook | None,
    ) -> tuple[dict[str, float], int]:
        """Worker thread: judge one batch, cache the answers, return them with the input token count."""
        books = [self.stats, call_stats]
        state = {**ctx, items_key: [it for _, it in batch]}
        questions = {
            f"r{i}": {"type": "noul", "instructions": instruction.format(ref=f"{items_key}[{i}]")}
            for i in range(len(batch))
        }
        body = json.dumps(
            {"model": self.settings.jev_model, "state": state, "questions": questions}, ensure_ascii=False, default=str
        ).encode()
        try:
            data, ms = self._post(body, label, call_stats)
            answers = data.get("answers") or {}
            missing = [i for i in range(len(batch)) if f"r{i}" not in answers]
            if missing:
                raise JevError(f"odata-jev: Jev response is missing answers ({len(answers)} of {len(batch)})")
            result: dict[str, float] = {}
            for i, (k, _) in enumerate(batch):
                a = answers[f"r{i}"]
                p = a.get("noul") if isinstance(a, dict) else None
                if not isinstance(p, int | float):
                    raise JevError(f"odata-jev: Jev answer r{i} has no noul probability: {str(a)[:200]}")
                result[k] = min(1.0, max(0.0, float(p)))
        except Exception:
            record_all(books, label, errors=1)
            raise
        usage = data.get("usage") or {}
        tin, tout = int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        record_all(
            books,
            label,
            requests=1,
            input_tokens=tin,
            output_tokens=tout,
            items=len(batch),
            api_ms=ms,
            cost_usd=tin * JEV_USD_PER_INPUT_TOKEN,
        )
        with self._lock:
            for k, p in result.items():
                self._cache[k] = p
                self._cache.move_to_end(k)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
        log.debug("jev[%s]: request with %d items, %d input tokens, %.0f ms", label, len(batch), tin, ms)
        return result, tin

    def _post(self, body: bytes, label: str, call_stats: StatsBook | None) -> tuple[dict[str, Any], float]:
        s = self.settings
        client = self._client()
        books = [self.stats, call_stats]
        delay = s.jev_retry_base_delay
        jitter = min(0.25, s.jev_retry_base_delay)
        last = ""
        for attempt in range(MAX_ATTEMPTS):
            t0 = time.monotonic()
            try:
                resp = client.post(s.jev_api_url, content=body)
            except httpx.TransportError as e:
                last = f"{type(e).__name__}: {e}"
                record_all(books, label, retries=1)
                if attempt > 0:  # the first retry is immediate: a pooled connection may just have gone stale
                    time.sleep(delay + random.random() * jitter)
                    delay = min(delay * 2, 8.0)
                continue
            ms = (time.monotonic() - t0) * 1000
            if resp.status_code == 200:
                try:
                    return resp.json(), ms
                except ValueError as e:
                    raise JevError(f"odata-jev: Jev returned invalid JSON: {resp.text[:200]}") from e
            last = f"{resp.status_code} {resp.text[:300]}"
            if resp.status_code in (408, 429, 529) or resp.status_code >= 500:
                record_all(books, label, retries=1)
                wait_s = _retry_after(resp)
                time.sleep(min(wait_s if wait_s is not None else delay, 30.0) + random.random() * jitter)
                delay = min(delay * 2, 8.0)
                continue
            raise JevError(f"odata-jev: TypeSafe API error {last}")
        raise JevError(f"odata-jev: TypeSafe API unreachable after {MAX_ATTEMPTS} attempts: {last}")
