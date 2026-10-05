"""Assemble the final request from validated query options.

Values are percent-encoded per RFC 3986 with the characters OData uses for structure left readable
(`$ , ( ) ' / : ; = @ ! *`). Spaces become %20 (never `+`, which some servers decode as a space and others keep),
and `+` in offsets such as `+02:00` becomes %2B.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel

ORDER = ("$filter", "$select", "$expand", "$orderby", "$top", "$skip", "$count", "$inlinecount", "$search", "search")
_SAFE = "$,()'/:;=@!*"


class BuiltQuery(BaseModel):
    entity_set: str
    url: str  # absolute when the service URL is known, else the same as `path`
    path: str  # EntitySet?query (encoded), relative to the service root
    query_string: str  # encoded, without the leading ?
    parts: dict[str, str]  # option → rendered value, not encoded
    encoded_parts: dict[str, str]  # option → encoded value

    @property
    def readable(self) -> str:
        """`EntitySet?$filter=a eq 'b'&$top=5` without percent-encoding, for prompts and logs."""
        q = "&".join(f"{k}={v}" for k, v in self.parts.items())
        return f"{self.entity_set}?{q}" if q else self.entity_set


def render(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def encode(value: str) -> str:
    return quote(value, safe=_SAFE)


def build_query(entity_set: str, query_options: Mapping[str, Any], service_url: str | None = None) -> BuiltQuery:
    keys = [k for k in ORDER if k in query_options] + [k for k in query_options if k not in ORDER]
    parts = {k: render(query_options[k]) for k in keys}
    encoded = {k: encode(v) for k, v in parts.items()}
    qs = "&".join(f"{encode(k)}={v}" for k, v in encoded.items())
    path = encode(entity_set) + (f"?{qs}" if qs else "")
    url = f"{service_url.rstrip('/')}/{path}" if service_url else path
    return BuiltQuery(entity_set=entity_set, url=url, path=path, query_string=qs, parts=parts, encoded_parts=encoded)
