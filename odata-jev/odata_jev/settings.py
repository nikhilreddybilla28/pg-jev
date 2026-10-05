"""Settings, read from environment variables by `Settings.from_env()` or passed directly.

Every field has an environment variable (listed in `ENV`). Keyword overrides win over the environment, which wins
over the defaults below.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .errors import ConfigError

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # Jev (TypeSafe System One API)
    typesafe_api_key: str | None = None
    jev_api_url: str = TYPESAFE_URL
    jev_model: str = "jev-latest"
    jev_batch_size: int = Field(20, ge=1, description="items per request; accuracy drops above ~20 (pg-jev)")
    jev_concurrency: int = Field(16, ge=1, description="parallel Jev requests")
    jev_timeout: float = Field(30.0, gt=0, description="seconds per Jev request")
    jev_keepalive: float = Field(600.0, ge=0, description="seconds an idle pooled connection is kept")
    jev_max_items_per_call: int = Field(0, ge=0, description="spend guard, 0 = off")
    jev_max_chars_per_call: int = Field(0, ge=0, description="spend guard, 0 = off")
    jev_retry_base_delay: float = Field(0.5, ge=0, description="first backoff in seconds, doubles up to 8 s")

    # LLM (OpenAI-compatible chat completions)
    llm_api_key: str | None = None
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str | None = None
    llm_temperature: float = Field(0.7, ge=0, le=2)
    llm_timeout: float = Field(60.0, gt=0)
    llm_json_mode: Literal["json_object", "json_schema", "none"] = "json_object"
    llm_concurrency: int = Field(4, ge=1)
    llm_seed: int | None = 0
    llm_price_input_per_mtok: float | None = Field(None, ge=0, description="USD per million prompt tokens")
    llm_price_output_per_mtok: float | None = Field(None, ge=0, description="USD per million completion tokens")
    llm_retry_base_delay: float = Field(0.5, ge=0)

    # Pipeline
    odata_version: Literal["v2", "v4"] = "v4"
    n_candidates: int = Field(3, ge=1, le=10)
    repair: bool = True
    selection_margin: float = Field(0.1, ge=0, le=1, description="keep the runner-up set when this close")
    selection_min_probability: float = Field(0.2, ge=0, le=1, description="warn below this")
    selection_max_properties: int = Field(60, ge=1, description="property names per entity set summary")
    prune_above: int = Field(30, ge=0, description="prune entity types with more properties + navigations")
    property_threshold: float = Field(0.3, ge=0, le=1)
    min_properties: int = Field(8, ge=0)
    max_properties: int = Field(60, ge=1)

    @field_validator("odata_version", mode="before")
    @classmethod
    def _version(cls, v: Any) -> Any:
        return normalize_version(v) if isinstance(v, str) else v

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **overrides: Any) -> Settings:
        env = os.environ if environ is None else environ
        values: dict[str, Any] = {}
        for field, var in ENV.items():
            raw = env.get(var)
            if raw is None or raw.strip() == "":
                continue
            values[field] = _coerce(field, raw.strip())
        values.update({k: v for k, v in overrides.items() if v is not None})
        try:
            return cls(**values)
        except ValueError as e:
            raise ConfigError(f"invalid settings: {e}") from e

    def require_jev_key(self) -> None:
        """TypeSafe hosts need a key; local Jev-compatible servers (stuntd, mocks) may run without one."""
        host = (urlsplit(self.jev_api_url).hostname or "").lower()
        if not self.typesafe_api_key and (host == "typesafe.ai" or host.endswith(".typesafe.ai")):
            raise ConfigError("odata-jev: no TypeSafe API key. Set TYPESAFE_API_KEY (https://console.typesafe.ai).")

    def require_llm(self) -> None:
        if not self.llm_model:
            raise ConfigError("odata-jev: no LLM model. Set LLM_MODEL (and LLM_BASE_URL / LLM_API_KEY if needed).")
        host = (urlsplit(self.llm_base_url).hostname or "").lower()
        if not self.llm_api_key and host.endswith("openai.com"):
            raise ConfigError("odata-jev: no LLM API key. Set LLM_API_KEY.")


ENV: dict[str, str] = {
    "typesafe_api_key": "TYPESAFE_API_KEY",
    "jev_api_url": "JEV_API_URL",
    "jev_model": "JEV_MODEL",
    "jev_batch_size": "JEV_BATCH_SIZE",
    "jev_concurrency": "JEV_CONCURRENCY",
    "jev_timeout": "JEV_TIMEOUT",
    "jev_keepalive": "JEV_KEEPALIVE",
    "jev_max_items_per_call": "JEV_MAX_ITEMS_PER_CALL",
    "jev_max_chars_per_call": "JEV_MAX_CHARS_PER_CALL",
    "jev_retry_base_delay": "JEV_RETRY_BASE_DELAY",
    "llm_api_key": "LLM_API_KEY",
    "llm_base_url": "LLM_BASE_URL",
    "llm_model": "LLM_MODEL",
    "llm_temperature": "LLM_TEMPERATURE",
    "llm_timeout": "LLM_TIMEOUT",
    "llm_json_mode": "LLM_JSON_MODE",
    "llm_concurrency": "LLM_CONCURRENCY",
    "llm_seed": "LLM_SEED",
    "llm_price_input_per_mtok": "LLM_PRICE_INPUT_PER_MTOK",
    "llm_price_output_per_mtok": "LLM_PRICE_OUTPUT_PER_MTOK",
    "llm_retry_base_delay": "LLM_RETRY_BASE_DELAY",
    "odata_version": "ODATA_VERSION",
    "n_candidates": "ODATA_JEV_CANDIDATES",
    "repair": "ODATA_JEV_REPAIR",
    "selection_margin": "ODATA_JEV_SELECTION_MARGIN",
    "selection_min_probability": "ODATA_JEV_SELECTION_MIN_PROBABILITY",
    "selection_max_properties": "ODATA_JEV_SELECTION_MAX_PROPERTIES",
    "prune_above": "ODATA_JEV_PRUNE_ABOVE",
    "property_threshold": "ODATA_JEV_PROPERTY_THRESHOLD",
    "min_properties": "ODATA_JEV_MIN_PROPERTIES",
    "max_properties": "ODATA_JEV_MAX_PROPERTIES",
}


def normalize_version(v: str) -> Literal["v2", "v4"]:
    s = str(v).strip().lower().lstrip("v")
    if s in ("2", "2.0", "3", "3.0"):
        return "v2"
    if s in ("4", "4.0", "4.01"):
        return "v4"
    raise ValueError(f"unknown OData version {v!r}; use v2 or v4")


def _coerce(field: str, raw: str) -> Any:
    if field == "llm_seed" and raw.lower() in ("none", "off", "null"):
        return None
    if field == "repair":
        return raw.lower() in ("1", "true", "on", "yes")
    return raw  # pydantic converts numeric strings
