"""Shared fixtures. Both mock servers run once per test session on 127.0.0.1 and are reset before each test."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from odata_jev.settings import Settings

from .mock_jev import MockJev
from .mock_llm import MockLLM

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def _jev_server() -> Iterator[MockJev]:
    m = MockJev()
    yield m
    m.close()


@pytest.fixture(scope="session")
def _llm_server() -> Iterator[MockLLM]:
    m = MockLLM()
    yield m
    m.close()


@pytest.fixture
def jev(_jev_server: MockJev) -> MockJev:
    _jev_server.reset()
    return _jev_server


@pytest.fixture
def llm(_llm_server: MockLLM) -> MockLLM:
    _llm_server.reset()
    return _llm_server


@pytest.fixture(autouse=True)
def _no_live_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never call live APIs, whatever the developer's shell exports."""
    for var in ("TYPESAFE_API_KEY", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "JEV_API_URL", "ODATA_VERSION"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def settings(jev: MockJev, llm: MockLLM) -> Settings:
    return Settings(
        typesafe_api_key="test-key",
        jev_api_url=jev.url,
        llm_api_key="llm-test-key",
        llm_base_url=llm.url,
        llm_model="mock-model",
        jev_retry_base_delay=0.0,
        llm_retry_base_delay=0.0,
    )
