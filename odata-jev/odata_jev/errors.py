"""Exceptions raised by odata-jev. Every one derives from OdataJevError."""

from __future__ import annotations

from typing import Any


class OdataJevError(Exception):
    """Base class for all odata-jev errors."""


class ConfigError(OdataJevError):
    """A setting is missing or invalid (no API key for TypeSafe, no LLM model, ...)."""


class MetadataError(OdataJevError):
    """The tool details or the $metadata document could not be read."""


class JevError(OdataJevError):
    """The Jev API returned an error or could not be reached after retries."""


class JevSpendLimitError(JevError):
    """A call would send more items or characters than JEV_MAX_ITEMS_PER_CALL / JEV_MAX_CHARS_PER_CALL allow."""


class LLMError(OdataJevError):
    """The chat completion API returned an error, could not be reached, or did not return JSON."""


class NoValidQueryError(OdataJevError):
    """No candidate survived validation and the repair round. `candidates` holds what was tried and why it failed."""

    def __init__(self, message: str, candidates: list[Any], stats: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.candidates = candidates
        self.stats = stats or {}
