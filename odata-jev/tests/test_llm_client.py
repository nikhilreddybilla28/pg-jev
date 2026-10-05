import json

import httpx
import pytest

from odata_jev.errors import ConfigError, LLMError
from odata_jev.llm_client import ChatRequest, LLMClient, extract_json
from odata_jev.stats import StatsBook

MSG = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


@pytest.fixture
def client(settings):
    c = LLMClient(settings)
    yield c
    c.close()


def test_request_shape_and_parsing(client, llm):
    llm.candidates = [{"entity_set": "A", "query_options": {}}]
    res = client.chat_json(ChatRequest(MSG, temperature=0.7, seed=3, label="generate"))
    assert res.data == {"entity_set": "A", "query_options": {}} and res.error is None
    body = llm.requests[0]
    assert body["model"] == "mock-model" and body["seed"] == 3 and body["temperature"] == 0.7
    assert body["response_format"] == {"type": "json_object"}
    assert llm.headers[0]["Authorization"] == "Bearer llm-test-key"
    t = client.stats.as_dict()["generate"]
    assert t["requests"] == 1 and t["input_tokens"] > 0 and t["estimated_cost_usd"] is None  # no price set


@pytest.mark.parametrize(
    "text, expected",
    [
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('Here you go: {"a": {"b": 2}} hope it helps', {"a": {"b": 2}}),
        ('{"a": 1}', {"a": 1}),
    ],
)
def test_extract_json(text, expected):
    assert extract_json(text) == expected


def test_non_json_reply_is_returned_with_error(client, llm):
    llm.candidates = ["sorry, I cannot do that"]
    res = client.chat_json(ChatRequest(MSG, temperature=0))
    assert res.data is None and "no JSON object" in res.error
    with pytest.raises(ValueError, match="expected a JSON object"):
        extract_json("[1, 2]")


def test_retries_and_errors(client, llm):
    llm.candidates = [{"ok": True}]
    llm.failures.extend([(429, {"Retry-After": "0"}), (503, {})])
    calls = StatsBook()
    assert client.chat_json(ChatRequest(MSG, temperature=0), call_stats=calls).data == {"ok": True}
    assert calls.total().retries == 2
    llm.failures.append((400, {}))
    with pytest.raises(LLMError, match="400"):
        client.chat_json(ChatRequest(MSG, temperature=0))


def test_json_modes_and_pricing(settings, llm):
    llm.candidates = [{"x": 1}]
    settings.llm_json_mode = "json_schema"
    settings.llm_price_input_per_mtok = 1.0
    settings.llm_price_output_per_mtok = 2.0
    settings.llm_seed = None
    with LLMClient(settings) as c:
        res = c.chat_json(ChatRequest(MSG, temperature=0), schema={"type": "object"})
        assert llm.requests[-1]["response_format"]["json_schema"]["schema"] == {"type": "object"}
        cost = c.stats.total().cost_usd
        assert cost == pytest.approx((res.prompt_tokens * 1.0 + res.completion_tokens * 2.0) / 1e6)
    settings.llm_json_mode = "none"
    with LLMClient(settings) as c:
        c.chat_json(ChatRequest(MSG, temperature=0))
        assert "response_format" not in llm.requests[-1] and "seed" not in llm.requests[-1]


def test_many_keeps_order_and_isolates_failures(client, llm):
    llm.handler = lambda body: "boom" if body["seed"] == 1 else {"seed": body["seed"]}
    results = client.chat_json_many([ChatRequest(MSG, temperature=0.7, seed=i) for i in range(4)])
    assert [r.data for r in results] == [{"seed": 0}, None, {"seed": 2}, {"seed": 3}]
    llm.handler = None
    llm.candidates = [{"ok": 1}]
    llm.failures.append((401, {}))
    results = client.chat_json_many([ChatRequest(MSG, temperature=0, seed=0)])
    assert isinstance(results[0], LLMError)


def test_requires_model(settings):
    settings.llm_model = None
    with LLMClient(settings) as c, pytest.raises(ConfigError, match="LLM_MODEL"):
        c.chat_json(ChatRequest(MSG, temperature=0))


def test_null_usage_and_unexpected_errors_do_not_sink_other_candidates(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        seed = json.loads(request.content)["seed"]
        if seed == 1:
            raise RuntimeError("boom")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"ok": 1}'}}],
                "usage": {"prompt_tokens": None, "completion_tokens": None},
            },
        )

    with LLMClient(settings, transport=httpx.MockTransport(handler)) as c:
        results = c.chat_json_many([ChatRequest(MSG, temperature=0.7, seed=i) for i in range(3)])
    assert results[0].data == {"ok": 1} and results[2].data == {"ok": 1}
    assert isinstance(results[1], LLMError) and "RuntimeError: boom" in str(results[1])


def test_extract_json_prefers_the_whole_reply_then_each_fence():
    assert extract_json('{"rationale": "use ```substringof``` here", "a": 1}')["a"] == 1
    assert extract_json('```text\nnot json\n```\n```json\n{"a": 2}\n```') == {"a": 2}


def test_no_sleep_after_the_last_attempt(client, llm):
    llm.failures.extend([(503, {"Retry-After": "0"})] * 5)
    calls = StatsBook()
    with pytest.raises(LLMError, match="after 5 attempts"):
        client.chat_json(ChatRequest(MSG, temperature=0), call_stats=calls)
    assert len(llm.requests) == 5 and calls.total().retries == 4
