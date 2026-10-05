"""odata-jev: text-to-OData with an LLM for generation and TypeSafe's Jev for calibrated judgments."""

__version__ = "0.1.0"

from .builder import BuiltQuery, build_query
from .errors import (
    ConfigError,
    JevError,
    JevSpendLimitError,
    LLMError,
    MetadataError,
    NoValidQueryError,
    OdataJevError,
)
from .metadata import EntitySet, EntityType, NavigationProperty, Property, Service, load_tools, parse_edmx
from .pipeline import (
    Result,
    RowJudgment,
    Session,
    extract_records,
    filter_rows,
    judge_rows,
    text_to_odata,
)
from .settings import Settings
from .validator import Issue, Validation, validate

__all__ = [
    "BuiltQuery",
    "ConfigError",
    "EntitySet",
    "EntityType",
    "Issue",
    "JevError",
    "JevSpendLimitError",
    "LLMError",
    "MetadataError",
    "NavigationProperty",
    "NoValidQueryError",
    "OdataJevError",
    "Property",
    "Result",
    "RowJudgment",
    "Service",
    "Session",
    "Settings",
    "Validation",
    "__version__",
    "build_query",
    "extract_records",
    "filter_rows",
    "judge_rows",
    "load_tools",
    "parse_edmx",
    "text_to_odata",
    "validate",
]
