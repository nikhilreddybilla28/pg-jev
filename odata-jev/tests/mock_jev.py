#!/usr/bin/env python3
"""Deterministic stand-in for https://api.typesafe.ai/v1/systemone.

Adapted from pg-jev's test/mock_api.py (https://github.com/realZachi/pg-jev, PostgreSQL License). Same contract:
POST {"model", "state", "questions": {id: {"type": "noul", "instructions": ...}}} → {"answers", "usage"}.

Rules, so expected output is stable:
  state has "condition" (judge_rows, pg-jev's row format)
        → noul 0.9 if the LAST word of the condition appears (case-insensitively) in the item JSON, else 0.1
  otherwise → keyword overlap between state["question"] and the item JSON: 0.05 + 0.9 × hits / keywords
  A question or condition containing "trigger422" returns HTTP 422 (non-retryable error path).
  usage.input_tokens = len(request body) // 4; usage.output_tokens = number of answers
  Auth: "Bearer test-key" is required, except under /local/ (a Jev-compatible server without auth).

Tests can set `server.rule = fn(ctx) -> float` to score items themselves, queue HTTP failures in
`server.failures` (status, headers), add `server.delay` seconds per request, and read `server.requests`,
`server.max_in_flight` and `server.client_ports` (distinct TCP connections).

Run: python3 tests/mock_jev.py [port]   (default 8765)
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

STOPWORDS = {
    "the",
    "and",
    "for",
    "are",
    "was",
    "were",
    "with",
    "that",
    "this",
    "which",
    "what",
    "who",
    "whose",
    "from",
    "all",
    "any",
    "have",
    "has",
    "had",
    "show",
    "list",
    "give",
    "find",
    "get",
    "me",
    "of",
    "in",
    "on",
    "by",
    "to",
    "is",
    "a",
    "an",
    "or",
    "how",
    "many",
    "much",
    "their",
    "them",
    "they",
    "its",
    "than",
    "more",
    "less",
}


@dataclass
class Ctx:
    state: dict[str, Any]
    items_key: str
    index: int
    item: Any
    item_json: str
    instructions: str


def keywords(text: str) -> list[str]:
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 3 and w not in STOPWORDS]
    return [w[:-1] if len(w) > 4 and w.endswith("s") else w for w in words]


def default_rule(ctx: Ctx) -> float:
    blob = ctx.item_json.lower()
    cond = ctx.state.get("condition")
    if isinstance(cond, str):
        words = cond.split()
        needle = words[-1].lower() if words else ""
        return 0.9 if needle and needle in blob else 0.1
    kws = keywords(str(ctx.state.get("question", "")))
    if not kws:
        return 0.5
    hits = sum(1 for w in kws if w in blob)
    return round(0.05 + 0.9 * hits / len(kws), 4)


class MockJev:
    def __init__(self, port: int = 0) -> None:
        self.rule: Callable[[Ctx], float] | None = None
        self.failures: deque[tuple[int, dict[str, str]]] = deque()
        self.delay = 0.0
        self.requests: list[dict[str, Any]] = []
        self.client_ports: set[int] = set()
        self.max_in_flight = 0
        self._in_flight = 0
        self._lock = threading.Lock()
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"  # keep-alive, so the client's connection reuse is exercised

            def log_message(self, *a: Any) -> None:
                pass

            def do_POST(self) -> None:
                mock._handle(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1/systemone"

    @property
    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/local/v1/systemone"

    def reset(self) -> None:
        with self._lock:
            self.rule = None
            self.failures.clear()
            self.delay = 0.0
            self.requests.clear()
            self.client_ports.clear()
            self.max_in_flight = 0

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _handle(self, h: BaseHTTPRequestHandler) -> None:
        body = h.rfile.read(int(h.headers.get("Content-Length", 0)))
        with self._lock:
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
            self.client_ports.add(h.client_address[1])
        try:
            if self.delay:
                time.sleep(self.delay)
            auth = h.headers.get("Authorization")
            if auth != "Bearer test-key" and not (h.path.startswith("/local/") and auth is None):
                return self._send(h, 401, {"error": "invalid api key"})
            req = json.loads(body)
            with self._lock:
                self.requests.append(req)
                failure = self.failures.popleft() if self.failures else None
            if failure is not None:
                status, headers = failure
                return self._send(h, status, {"error": "mock failure"}, headers)
            state, questions = req["state"], req["questions"]
            text = json.dumps(state.get("condition", "")) + json.dumps(state.get("question", ""))
            if "trigger422" in text:
                return self._send(h, 422, {"error": "mock validation failure"})
            rule = self.rule or default_rule
            answers = {}
            for qid, q in questions.items():
                instructions = q.get("instructions", "")
                ref = re.search(r"`(\w+)\[(\d+)\]`", instructions)  # "Does the record `rows[3]` satisfy ..."
                items_key, i = (ref.group(1), int(ref.group(2))) if ref else ("rows", int(re.sub(r"\D", "", qid)))
                item = state[items_key][i]
                item_json = json.dumps(item, sort_keys=True, ensure_ascii=False)
                answers[qid] = {"type": "noul", "noul": rule(Ctx(state, items_key, i, item, item_json, instructions))}
            self._send(
                h,
                200,
                {
                    "model": "jev-mock",
                    "answers": answers,
                    "usage": {"input_tokens": len(body) // 4, "output_tokens": len(answers)},
                },
            )
        finally:
            with self._lock:
                self._in_flight -= 1

    @staticmethod
    def _send(h: BaseHTTPRequestHandler, code: int, obj: Any, headers: dict[str, str] | None = None) -> None:
        data = json.dumps(obj).encode()
        h.send_response(code)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(data)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    m = MockJev(port)
    print(f"mock Jev on {m.url}")
    m.thread.join()
