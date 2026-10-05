import pytest

from odata_jev.errors import ConfigError
from odata_jev.settings import Settings, normalize_version


def test_defaults():
    s = Settings()
    assert (s.jev_batch_size, s.jev_concurrency, s.jev_timeout, s.jev_model) == (20, 16, 30.0, "jev-latest")
    assert (s.odata_version, s.n_candidates, s.llm_json_mode) == ("v4", 3, "json_object")


def test_from_env_and_overrides():
    env = {
        "TYPESAFE_API_KEY": "k",
        "LLM_API_KEY": "l",
        "LLM_BASE_URL": "http://localhost:11434/v1",
        "LLM_MODEL": "qwen",
        "JEV_MODEL": "jev-1.13.0",
        "JEV_BATCH_SIZE": "10",
        "JEV_CONCURRENCY": "4",
        "JEV_TIMEOUT": "12.5",
        "ODATA_VERSION": "2.0",
        "LLM_SEED": "none",
        "ODATA_JEV_REPAIR": "off",
        "LLM_TEMPERATURE": "",
    }
    s = Settings.from_env(env, llm_model="override", jev_concurrency=None)
    assert s.typesafe_api_key == "k" and s.llm_model == "override" and s.jev_model == "jev-1.13.0"
    assert (s.jev_batch_size, s.jev_concurrency, s.jev_timeout) == (10, 4, 12.5)
    assert s.odata_version == "v2" and s.llm_seed is None and s.repair is False and s.llm_temperature == 0.7


@pytest.mark.parametrize(
    "env", [{"JEV_BATCH_SIZE": "0"}, {"JEV_TIMEOUT": "soon"}, {"ODATA_VERSION": "v3.5"}, {"LLM_JSON_MODE": "xml"}]
)
def test_invalid_env(env):
    with pytest.raises(ConfigError, match="invalid settings"):
        Settings.from_env(env)


def test_key_rules():
    with pytest.raises(ConfigError, match="TYPESAFE_API_KEY"):
        Settings().require_jev_key()
    Settings(jev_api_url="http://127.0.0.1:8787/v1/systemone").require_jev_key()  # local server, no key needed
    with pytest.raises(ConfigError, match="LLM_MODEL"):
        Settings().require_llm()
    with pytest.raises(ConfigError, match="LLM_API_KEY"):
        Settings(llm_model="gpt").require_llm()
    Settings(llm_model="llama", llm_base_url="http://localhost:11434/v1").require_llm()


def test_normalize_version():
    assert [normalize_version(v) for v in ("v2", "V4", "4.01", "2", "3.0")] == ["v2", "v4", "v4", "v2", "v2"]
    with pytest.raises(ValueError):
        normalize_version("5")
