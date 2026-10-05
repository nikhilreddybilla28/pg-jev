"""Thin client for an OpenAI-compatible `POST {base}/chat/completions` endpoint that returns JSON objects.

`LLM_JSON_MODE` picks how JSON is requested: `json_object` (response_format {"type": "json_object"}, the most
widely supported), `json_schema` (structured outputs with the schema the caller passes) or `none` (prompt only,
for servers that reject response_format). Whatever the mode, the reply is parsed leniently: code fences and
text around the outermost {...} are stripped.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import httpx

from . import __version__
from .errors import ConfigError, LLMError
from .settings import Settings
from .stats import StatsBook, record_all

log = logging.getLogger("odata_jev.llm")

MAX_ATTEMPTS = 5


@dataclass
class ChatRequest:
    messages: list[dict[str, str]]
    temperature: float
    seed: int | None = None
    label: str = "llm"


@dataclass
class ChatResult:
    content: str
    data: dict[str, Any] | None  # parsed JSON object, None when the reply was not JSON
    error: str | None  # why `data` is None
    prompt_tokens: int
    completion_tokens: int


def extract_json(text: str) -> dict[str, Any]:
    """Parse a JSON object from a reply: the whole reply, else each ``` fence in turn, else the outermost {...}."""
    t = text.strip()
    attempts = [t] + [m.group(1).strip() for m in re.finditer(r"```(?:json)?\s*(.*?)```", t, re.S | re.I)]
    start, end = t.find("{"), t.rfind("}")
    if 0 <= start < end:
        attempts.append(t[start : end + 1])
    error = "no JSON object in the reply"
    for candidate in attempts:
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError as e:
            if candidate.startswith("{"):
                error = f"invalid JSON: {e}"
            continue
        if isinstance(obj, dict):
            return obj
        error = f"expected a JSON object, got {type(obj).__name__}"
    raise ValueError(error)


class LLMClient:
    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        priced = settings.llm_price_input_per_mtok is not None and settings.llm_price_output_per_mtok is not None
        self.stats = StatsBook(priced=priced)
        self.priced = priced  # False: no LLM prices configured, costs are reported as unknown (None)
        self._transport = transport
        self._lock = threading.Lock()
        self._http: httpx.Client | None = None
        self._pool: ThreadPoolExecutor | None = None

    def _client(self) -> httpx.Client:
        with self._lock:
            if self._http is None:
                s = self.settings
                headers = {"Content-Type": "application/json", "User-Agent": f"odata-jev/{__version__}"}
                if s.llm_api_key:
                    headers["Authorization"] = f"Bearer {s.llm_api_key}"
                limits = httpx.Limits(max_connections=s.llm_concurrency, max_keepalive_connections=s.llm_concurrency)
                self._http = httpx.Client(
                    transport=self._transport, limits=limits, headers=headers, timeout=httpx.Timeout(s.llm_timeout)
                )
            return self._http

    def _executor(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=self.settings.llm_concurrency, thread_name_prefix="llm")
            return self._pool

    def close(self) -> None:
        with self._lock:
            if self._pool is not None:
                self._pool.shutdown(wait=False, cancel_futures=True)
                self._pool = None
            if self._http is not None:
                self._http.close()
                self._http = None

    def __enter__(self) -> LLMClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------------------------------ public API
    def chat_json(
        self,
        req: ChatRequest,
        *,
        schema: dict[str, Any] | None = None,
        call_stats: StatsBook | None = None,
    ) -> ChatResult:
        """One chat completion. HTTP failures raise LLMError; a reply that is not JSON comes back with `error` set
        (the generator turns that into a validation issue for the repair round)."""
        s = self.settings
        s.require_llm()
        body: dict[str, Any] = {"model": s.llm_model, "messages": req.messages, "temperature": req.temperature}
        if req.seed is not None:
            body["seed"] = req.seed
        if s.llm_json_mode == "json_object":
            body["response_format"] = {"type": "json_object"}
        elif s.llm_json_mode == "json_schema" and schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "odata_query", "schema": schema}}
        books = [self.stats, call_stats]
        data, ms = self._post(body, req.label, books)
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            record_all(books, req.label, errors=1)
            raise LLMError(f"odata-jev: unexpected chat completion response: {str(data)[:300]}") from e
        usage = data.get("usage") or {}
        pt, ct = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        cost = None
        if self.priced:
            cost = (pt * (s.llm_price_input_per_mtok or 0) + ct * (s.llm_price_output_per_mtok or 0)) / 1_000_000
        record_all(books, req.label, requests=1, input_tokens=pt, output_tokens=ct, items=1, api_ms=ms, cost_usd=cost)
        log.info(
            "llm[%s]: %s, %d prompt + %d completion tokens%s, %.0f ms",
            req.label,
            s.llm_model,
            pt,
            ct,
            "" if cost is None else f" (≈${cost:.6f})",
            ms,
        )
        try:
            return ChatResult(content, extract_json(content), None, pt, ct)
        except ValueError as e:
            return ChatResult(content, None, str(e), pt, ct)

    def chat_json_many(
        self,
        reqs: Sequence[ChatRequest],
        *,
        schema: dict[str, Any] | None = None,
        call_stats: StatsBook | None = None,
    ) -> list[ChatResult | LLMError]:
        """Run several chats concurrently (up to LLM_CONCURRENCY). Results keep the input order; a failed call
        yields its LLMError in place, so one bad call does not discard the others."""
        if not reqs:
            return []
        self.settings.require_llm()
        pool = self._executor()
        futures = [pool.submit(self.chat_json, r, schema=schema, call_stats=call_stats) for r in reqs]
        out: list[ChatResult | LLMError] = []
        try:
            for f in futures:
                while True:
                    try:
                        out.append(f.result(timeout=0.25))
                        break
                    except TimeoutError:
                        continue
                    except LLMError as e:
                        out.append(e)
                        break
                    except ConfigError:
                        raise
                    except Exception as e:  # one malformed reply must not discard the other candidates
                        out.append(LLMError(f"odata-jev: chat call failed: {type(e).__name__}: {e}"))
                        break
        except BaseException:
            for f in futures:
                f.cancel()
            raise
        return out

    # ------------------------------------------------------------------------------------------ HTTP
    def _post(self, body: dict[str, Any], label: str, books: list[StatsBook | None]) -> tuple[dict[str, Any], float]:
        s = self.settings
        url = s.llm_base_url.rstrip("/") + "/chat/completions"
        client = self._client()
        delay = s.llm_retry_base_delay
        jitter = min(0.25, s.llm_retry_base_delay)
        last = ""
        for attempt in range(MAX_ATTEMPTS):
            final = attempt == MAX_ATTEMPTS - 1
            t0 = time.monotonic()
            try:
                resp = client.post(url, json=body)
            except httpx.TransportError as e:
                last = f"{type(e).__name__}: {e}"
                if final:
                    break
                record_all(books, label, retries=1)
                if attempt > 0:
                    time.sleep(delay + random.random() * jitter)
                    delay = min(delay * 2, 8.0)
                continue
            ms = (time.monotonic() - t0) * 1000
            if resp.status_code == 200:
                try:
                    return resp.json(), ms
                except ValueError as e:
                    record_all(books, label, errors=1)
                    raise LLMError(f"odata-jev: LLM returned invalid JSON: {resp.text[:200]}") from e
            last = f"{resp.status_code} {resp.text[:300]}"
            if resp.status_code in (408, 409, 429) or resp.status_code >= 500:
                if final:
                    break
                record_all(books, label, retries=1)
                wait_s = None
                ra = resp.headers.get("retry-after")
                if ra:
                    try:
                        wait_s = float(ra)
                    except ValueError:
                        wait_s = None
                time.sleep(min(wait_s if wait_s is not None else delay, 30.0) + random.random() * jitter)
                delay = min(delay * 2, 8.0)
                continue
            record_all(books, label, errors=1)
            raise LLMError(f"odata-jev: LLM API error {last}")
        record_all(books, label, errors=1)
        raise LLMError(f"odata-jev: LLM API unreachable after {MAX_ATTEMPTS} attempts: {last}")
