#!/usr/bin/env python3
"""Minimal OpenAI-compatible /v1/chat/completions server that returns canned JSON, so tests never call an LLM.

* Generation calls (no assistant turn in `messages`) answer `candidates[seed % len(candidates)]`, so candidate i
  of a run (sent with seed i) always gets the same canned answer, whatever order the concurrent calls arrive in.
* Repair calls (the conversation already holds an assistant turn) pop answers from the `repairs` FIFO.
* `handler(body) -> dict | str` overrides both. Dicts are serialised as the message content; strings are sent
  as they are (to test fenced or broken JSON).
* `failures` queues HTTP errors (status, headers); `requests` records every request body.
* usage.prompt_tokens = len(json(messages)) // 4, usage.completion_tokens = len(content) // 4.

Run: python3 tests/mock_llm.py [port]   (default 8766), then LLM_BASE_URL=http://127.0.0.1:8766/v1
"""

from __future__ import annotations

import json
import sys
import threading
from collections import deque
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

Answer = dict[str, Any] | str


class MockLLM:
    def __init__(self, port: int = 0) -> None:
        self.candidates: list[Answer] = []
        self.repairs: deque[Answer] = deque()
        self.handler: Callable[[dict[str, Any]], Answer] | None = None
        self.failures: deque[tuple[int, dict[str, str]]] = deque()
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self._lock = threading.Lock()
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

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
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def reset(self) -> None:
        with self._lock:
            self.candidates = []
            self.repairs.clear()
            self.handler = None
            self.failures.clear()
            self.requests.clear()
            self.headers.clear()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def generation_requests(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if not is_repair(r)]

    def repair_requests(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if is_repair(r)]

    def _handle(self, h: BaseHTTPRequestHandler) -> None:
        body = json.loads(h.rfile.read(int(h.headers.get("Content-Length", 0))))
        if not h.path.endswith("/chat/completions"):
            return self._send(h, 404, {"error": {"message": "not found"}})
        with self._lock:
            self.requests.append(body)
            self.headers.append(dict(h.headers))
            failure = self.failures.popleft() if self.failures else None
            if failure is None:
                if self.handler is not None:
                    answer: Answer | None = self.handler(body)
                elif is_repair(body):
                    answer = self.repairs.popleft() if self.repairs else None
                elif self.candidates:
                    answer = self.candidates[int(body.get("seed") or 0) % len(self.candidates)]
                else:
                    answer = None
        if failure is not None:
            status, headers = failure
            return self._send(h, status, {"error": {"message": "mock failure"}}, headers)
        if answer is None:
            return self._send(h, 500, {"error": {"message": "mock LLM has no canned answer for this request"}})
        content = answer if isinstance(answer, str) else json.dumps(answer)
        prompt_tokens = len(json.dumps(body.get("messages", []))) // 4
        self._send(
            h,
            200,
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "model": body.get("model"),
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": len(content) // 4,
                    "total_tokens": prompt_tokens + len(content) // 4,
                },
            },
        )

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


def is_repair(body: dict[str, Any]) -> bool:
    return any(m.get("role") == "assistant" for m in body.get("messages", []))


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8766
    m = MockLLM(port)
    m.candidates = [
        {
            "entity_set": "Customers",
            "query_options": {"$top": 5},
            "residual_condition": None,
            "rationale": "mock answer",
        }
    ]
    print(f"mock LLM on {m.url}")
    m.thread.join()
