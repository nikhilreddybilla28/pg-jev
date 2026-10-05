import json

import httpx
import pytest

from odata_jev.errors import ConfigError, JevError, JevSpendLimitError
from odata_jev.jev_client import JevClient
from odata_jev.stats import JEV_USD_PER_INPUT_TOKEN, StatsBook

ROW_Q = "Does the record `{ref}` satisfy the condition stated in `condition`?"  # pg-jev's question


@pytest.fixture
def client(settings):
    c = JevClient(settings)
    yield c
    c.close()


def test_batches_of_20_in_pg_jev_format(client, jev):
    jev.rule = lambda ctx: ctx.item["n"] / 100
    items = [{"n": n} for n in range(45)]
    probs = client.noul(items, instruction=ROW_Q, context={"condition": "x"}, items_key="rows")
    assert probs == [n / 100 for n in range(45)]
    assert sorted(len(r["state"]["rows"]) for r in jev.requests) == [5, 20, 20]
    req = jev.requests[0]
    assert req["model"] == "jev-latest"
    assert req["state"]["condition"] == "x"
    assert req["questions"]["r3"] == {
        "type": "noul",
        "instructions": "Does the record `rows[3]` satisfy the condition stated in `condition`?",
    }


def test_cache_and_duplicates(client, jev):
    items = [{"name": "Anna"}, {"name": "Bob"}, {"name": "Anna"}]
    first = client.noul(items, instruction=ROW_Q, context={"condition": "the name is anna"}, items_key="rows")
    assert first == [0.9, 0.1, 0.9]  # mock: last word of the condition in the row
    assert len(jev.requests) == 1 and len(jev.requests[0]["state"]["rows"]) == 2  # duplicate sent once
    calls = StatsBook()
    again = client.noul(
        items, instruction=ROW_Q, context={"condition": "the name is anna"}, items_key="rows", call_stats=calls
    )
    assert again == first and len(jev.requests) == 1
    assert calls.total().cache_hits == 3 and calls.total().requests == 0
    other = client.noul(items[:1], instruction=ROW_Q, context={"condition": "the name is bob"}, items_key="rows")
    assert other == [0.1] and len(jev.requests) == 2  # another question is another cache entry
    assert client.cached_answers == 3
    client.cache_clear()
    assert client.cached_answers == 0


def test_concurrency_limit_and_keepalive_reuse(settings, jev):
    settings.jev_concurrency = 3
    jev.delay = 0.05
    with JevClient(settings) as c:
        c.noul([{"n": n} for n in range(200)], instruction="Is `{ref}` even?", context={"question": "even"})
        c.noul([{"m": n} for n in range(200)], instruction="Is `{ref}` even?", context={"question": "even"})
    assert len(jev.requests) == 20
    assert 1 < jev.max_in_flight <= 3
    assert len(jev.client_ports) <= 3  # 20 requests over at most 3 pooled connections


def test_retries_honour_retry_after(client, jev):
    jev.failures.extend([(503, {"Retry-After": "0"}), (429, {"retry-after-ms": "0"}), (529, {})])
    calls = StatsBook()
    assert client.noul([{"a": 1}], instruction="Is `{ref}` fine?", context={"question": "q"}, call_stats=calls) == [
        pytest.approx(0.5)
    ]
    assert calls.total().retries == 3 and calls.total().requests == 1
    assert len(jev.requests) == 4


def test_non_retryable_and_auth_errors(settings, jev):
    with JevClient(settings) as c, pytest.raises(JevError, match="422"):
        c.noul([{"a": 1}], instruction="Is `{ref}` ok?", context={"question": "trigger422"})
    assert len(jev.requests) == 1  # no retry
    assert client_stats_errors(c) == 1
    settings.typesafe_api_key = "wrong"
    with JevClient(settings) as c, pytest.raises(JevError, match="401"):
        c.noul([{"a": 1}], instruction="Is `{ref}` ok?", context={"question": "q"})


def client_stats_errors(c: JevClient) -> int:
    return c.stats.total().errors


def test_api_key_rules(settings, jev):
    settings.typesafe_api_key = None
    settings.jev_api_url = "https://api.typesafe.ai/v1/systemone"
    with JevClient(settings) as c, pytest.raises(ConfigError, match="TYPESAFE_API_KEY"):
        c.noul([{"a": 1}], instruction="Is `{ref}` ok?", context={"question": "q"})
    settings.jev_api_url = jev.local_url  # a local Jev-compatible server needs no key
    with JevClient(settings) as c:
        assert c.noul([{"a": 1}], instruction="Is `{ref}` ok?", context={"question": "q"}) == [0.5]


def test_spend_guard(settings, jev):
    settings.jev_max_items_per_call = 10
    with JevClient(settings) as c:
        with pytest.raises(JevSpendLimitError, match=r"25 items .* JEV_MAX_ITEMS_PER_CALL = 10"):
            c.noul([{"n": n} for n in range(25)], instruction="Is `{ref}` ok?", context={"question": "q"})
        assert jev.requests == []
        settings.jev_max_items_per_call = 0
        settings.jev_max_chars_per_call = 50
        with pytest.raises(JevSpendLimitError, match="JEV_MAX_CHARS_PER_CALL"):
            c.noul([{"text": "x" * 100}], instruction="Is `{ref}` ok?", context={"question": "q"})


def test_stats_tokens_and_cost(client, jev):
    calls = StatsBook()
    client.noul(
        [{"n": n} for n in range(30)],
        instruction="Is `{ref}` ok?",
        context={"question": "q"},
        label="select",
        call_stats=calls,
    )
    expected_tokens = sum(len(json.dumps(r, ensure_ascii=False)) // 4 for r in jev.requests)
    d = calls.as_dict()
    assert d["select"]["requests"] == 2 and d["select"]["items"] == 30
    assert abs(d["select"]["input_tokens"] - expected_tokens) <= 4  # body formatting may differ by a few bytes
    assert d["total"]["estimated_cost_usd"] == pytest.approx(d["select"]["input_tokens"] * JEV_USD_PER_INPUT_TOKEN)
    assert client.stats.total().requests == 2  # the session book counts too


def test_malformed_response_is_an_error(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"answers": {"r0": {"type": "noul", "noul": 0.7}}, "usage": {}})

    with JevClient(settings, transport=httpx.MockTransport(handler)) as c:
        assert c.noul([1], instruction="Is `{ref}` ok?") == [0.7]
        with pytest.raises(JevError, match="missing answers"):
            c.noul([2, 3], instruction="Is `{ref}` ok?")


def test_argument_checks(client):
    with pytest.raises(ValueError, match="ref"):
        client.noul([1], instruction="no placeholder")
    with pytest.raises(ValueError, match="items key"):
        client.noul([1], instruction="Is `{ref}` ok?", context={"items": []})
    assert client.noul([], instruction="Is `{ref}` ok?") == []


def test_null_usage_is_zero(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "answers": {"r0": {"type": "noul", "noul": 0.6}},
                "usage": {"input_tokens": None, "output_tokens": None},
            },
        )

    with JevClient(settings, transport=httpx.MockTransport(handler)) as c:
        assert c.noul([1], instruction="Is `{ref}` ok?") == [0.6]
        assert c.stats.total().input_tokens == 0


def test_retries_stop_after_the_last_attempt(client, jev):
    jev.failures.extend([(503, {"Retry-After": "0"})] * 7)
    calls = StatsBook()
    with pytest.raises(JevError, match="after 7 attempts"):
        client.noul([{"a": 1}], instruction="Is `{ref}` ok?", context={"question": "q"}, call_stats=calls)
    assert len(jev.requests) == 7 and calls.total().retries == 6
