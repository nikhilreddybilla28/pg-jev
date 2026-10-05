"""What differs between OData V2 and V4: query options, functions, literal forms, and the prompt cheat sheets.

The validator and the generator both read these tables, so the prompt never teaches syntax the validator rejects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Version = Literal["v2", "v4"]
Family = (
    str  # "string", "numeric", "bool", "date", "datetime", "time", "duration", "guid", "binary", "enum", "geo", "any"
)

PROTOCOL = {"v2": "2.0", "v4": "4.0"}

QUERY_OPTIONS: dict[str, tuple[str, ...]] = {
    "v2": ("$filter", "$select", "$expand", "$orderby", "$top", "$skip", "$inlinecount", "search"),
    "v4": ("$filter", "$select", "$expand", "$orderby", "$top", "$skip", "$count", "$search"),
}

EDM_FAMILY: dict[str, Family] = {
    "Edm.String": "string",
    "Edm.Byte": "numeric",
    "Edm.SByte": "numeric",
    "Edm.Int16": "numeric",
    "Edm.Int32": "numeric",
    "Edm.Int64": "numeric",
    "Edm.Decimal": "numeric",
    "Edm.Double": "numeric",
    "Edm.Single": "numeric",
    "Edm.Boolean": "bool",
    "Edm.Date": "date",
    "Edm.DateTime": "datetime",
    "Edm.DateTimeOffset": "datetime",
    "Edm.Time": "time",
    "Edm.TimeOfDay": "time",
    "Edm.Duration": "duration",
    "Edm.Guid": "guid",
    "Edm.Binary": "binary",
    "Edm.Stream": "stream",
}


def edm_family(edm_type: str) -> Family:
    if edm_type.startswith(("Edm.Geography", "Edm.Geometry")):
        return "geo"
    return EDM_FAMILY.get(edm_type, "any")


@dataclass(frozen=True)
class Function:
    name: str
    min_args: int
    max_args: int
    returns: Family  # "arg0" = same family as the first argument
    arg_family: Family | None = None  # required family of the "subject" argument, if any
    subject: int = 0  # which argument is the subject (substringof searches in its 2nd argument)
    versions: tuple[Version, ...] = ("v2", "v4")
    note: str = ""


_FUNCS = [
    Function("substringof", 2, 2, "bool", "string", 1, ("v2",), "substringof('needle', Property): needle first"),
    Function("contains", 2, 2, "bool", "string", 0, ("v4",)),
    Function("startswith", 2, 2, "bool", "string"),
    Function("endswith", 2, 2, "bool", "string"),
    Function("length", 1, 1, "numeric", "string"),
    Function("indexof", 2, 2, "numeric", "string"),
    Function("substring", 2, 3, "string", "string"),
    Function("replace", 3, 3, "string", "string", 0, ("v2",)),
    Function("tolower", 1, 1, "string", "string"),
    Function("toupper", 1, 1, "string", "string"),
    Function("trim", 1, 1, "string", "string"),
    Function("concat", 2, 2, "string"),
    Function("matchespattern", 2, 2, "bool", "string", 0, ("v4",), "OData 4.01"),
    Function("year", 1, 1, "numeric", "temporal"),
    Function("month", 1, 1, "numeric", "temporal"),
    Function("day", 1, 1, "numeric", "temporal"),
    Function("hour", 1, 1, "numeric", "temporal"),
    Function("minute", 1, 1, "numeric", "temporal"),
    Function("second", 1, 1, "numeric", "temporal"),
    Function("fractionalseconds", 1, 1, "numeric", "temporal", 0, ("v4",)),
    Function("totaloffsetminutes", 1, 1, "numeric", "datetime", 0, ("v4",)),
    Function("totalseconds", 1, 1, "numeric", "duration", 0, ("v4",)),
    Function("date", 1, 1, "date", "datetime", 0, ("v4",)),
    Function("time", 1, 1, "time", "datetime", 0, ("v4",)),
    Function("now", 0, 0, "datetime", None, 0, ("v4",)),
    Function("mindatetime", 0, 0, "datetime", None, 0, ("v4",)),
    Function("maxdatetime", 0, 0, "datetime", None, 0, ("v4",)),
    Function("round", 1, 1, "numeric", "numeric"),
    Function("floor", 1, 1, "numeric", "numeric"),
    Function("ceiling", 1, 1, "numeric", "numeric"),
    Function("isof", 1, 2, "bool"),
    Function("cast", 1, 2, "any"),
    Function("geo.distance", 2, 2, "numeric", None, 0, ("v4",)),
    Function("geo.length", 1, 1, "numeric", None, 0, ("v4",)),
    Function("geo.intersects", 2, 2, "bool", None, 0, ("v4",)),
]
FUNCTIONS: dict[str, Function] = {f.name: f for f in _FUNCS}

# Literal token kinds (see expression.py) → (family, versions that accept the form)
LITERALS: dict[str, tuple[Family, tuple[Version, ...]]] = {
    "string": ("string", ("v2", "v4")),
    "int": ("numeric", ("v2", "v4")),
    "decimal": ("numeric", ("v2", "v4")),
    "double": ("numeric", ("v2", "v4")),
    "int64_suffix": ("numeric", ("v2",)),
    "decimal_suffix": ("numeric", ("v2",)),
    "double_suffix": ("numeric", ("v2",)),
    "single_suffix": ("numeric", ("v2",)),
    "bool": ("bool", ("v2", "v4")),
    "null": ("null", ("v2", "v4")),
    "datetime_v2": ("datetime", ("v2",)),
    "datetimeoffset_v2": ("datetime", ("v2",)),
    "time_v2": ("time", ("v2",)),
    "guid_v2": ("guid", ("v2",)),
    "binary": ("binary", ("v2", "v4")),
    "date": ("date", ("v4",)),
    "datetimeoffset": ("datetime", ("v4",)),
    "datetime_no_offset": ("datetime", ()),  # 2024-01-31T10:00:00 without Z/offset: valid nowhere
    "timeofday": ("time", ("v4",)),
    "guid": ("guid", ("v4",)),
    "duration": ("duration", ("v4",)),
    "enum": ("enum", ("v4",)),
    "geo": ("geo", ("v2", "v4")),
}

LITERAL_HINT: dict[tuple[str, Version], str] = {
    ("datetime_v2", "v4"): "V4 has no datetime'…' literal: write 2024-01-31 for Edm.Date or 2024-01-31T00:00:00Z",
    ("datetimeoffset_v2", "v4"): "V4 has no datetimeoffset'…' literal: write 2024-01-31T00:00:00Z",
    ("time_v2", "v4"): "V4 has no time'…' literal: write 13:20:00 for Edm.TimeOfDay or duration'PT13H20M'",
    ("guid_v2", "v4"): "V4 GUID literals are unquoted: 01234567-89ab-cdef-0123-456789abcdef",
    ("date", "v2"): "V2 has no date literal: write datetime'2024-01-31T00:00:00'",
    ("datetimeoffset", "v2"): "V2 date-time literals are prefixed: datetime'2024-01-31T00:00:00' or "
    "datetimeoffset'2024-01-31T00:00:00Z'",
    ("datetime_no_offset", "v2"): "V2 date-time literals are prefixed: datetime'2024-01-31T00:00:00'",
    ("datetime_no_offset", "v4"): "V4 Edm.DateTimeOffset literals need a time zone: 2024-01-31T00:00:00Z",
    ("timeofday", "v2"): "V2 Edm.Time literals look like time'PT13H20M'",
    ("guid", "v2"): "V2 GUID literals are prefixed: guid'01234567-89ab-cdef-0123-456789abcdef'",
    ("duration", "v2"): "V2 has no duration literal",
    ("enum", "v2"): "V2 has no enum types",
    ("int64_suffix", "v4"): "V4 numbers take no L suffix",
    ("decimal_suffix", "v4"): "V4 numbers take no M suffix",
    ("double_suffix", "v4"): "V4 numbers take no d suffix",
    ("single_suffix", "v4"): "V4 numbers take no f suffix",
}

# How a literal must look to compare with a property of a given type, by version (used in error messages)
LITERAL_EXAMPLE: dict[tuple[str, Version], str] = {
    ("Edm.String", "v2"): "'text'",
    ("Edm.String", "v4"): "'text'",
    ("Edm.DateTime", "v2"): "datetime'2024-01-31T00:00:00'",
    ("Edm.DateTime", "v4"): "2024-01-31T00:00:00Z",
    ("Edm.DateTimeOffset", "v2"): "datetimeoffset'2024-01-31T00:00:00Z'",
    ("Edm.DateTimeOffset", "v4"): "2024-01-31T00:00:00Z",
    ("Edm.Date", "v4"): "2024-01-31",
    ("Edm.Date", "v2"): "datetime'2024-01-31T00:00:00'",
    ("Edm.Time", "v2"): "time'PT13H20M'",
    ("Edm.TimeOfDay", "v4"): "13:20:00",
    ("Edm.Guid", "v2"): "guid'01234567-89ab-cdef-0123-456789abcdef'",
    ("Edm.Guid", "v4"): "01234567-89ab-cdef-0123-456789abcdef",
    ("Edm.Boolean", "v2"): "true",
    ("Edm.Boolean", "v4"): "true",
}


def literal_example(edm_type: str, version: Version) -> str | None:
    if edm_family(edm_type) == "numeric":
        return "42 or 42.5 (unquoted)"
    return LITERAL_EXAMPLE.get((edm_type, version))


CHEAT_SHEET: dict[Version, str] = {
    "v4": """\
OData V4 syntax (use exactly this; V2 forms such as substringof, datetime'…' or $inlinecount are invalid here)
- $filter operators: eq ne gt ge lt le, and or not, add sub mul div mod, parentheses for grouping.
- Strings in single quotes, a quote inside a string is doubled: 'O''Neil'. Names are case-sensitive.
- String functions: contains(Name,'abc'), startswith(Name,'abc'), endswith(Name,'abc'), tolower(Name) eq 'abc',
  toupper, trim, length(Name) gt 3, indexof, substring, concat.
- Date/time functions: year(OrderDate) eq 2024, month, day, hour, minute, second, date(CreatedAt), now().
- Math: round, floor, ceiling.
- Literals: Edm.Date 2024-01-31 | Edm.DateTimeOffset 2024-01-31T10:00:00Z | Edm.TimeOfDay 10:00:00 |
  Edm.Guid 01234567-89ab-cdef-0123-456789abcdef (unquoted) | numbers 42 / 42.5 (no suffix) | true false null |
  enum members Namespace.EnumType'Member' or 'Member'.
- Single-valued navigation: Customer/Country eq 'DE'. Collection navigation needs a lambda:
  Items/any(i: i/Material eq 'M-01'), Items/all(i: i/Quantity gt 0).
- $select=ID,Name,NetAmount  (comma-separated, no spaces needed)
- $orderby=NetAmount desc,OrderDate asc
- $top=10  $skip=20  (integers)
- $count=true  adds the total count to the response; for "how many" questions use $count=true with $top=0.
- $expand=Items  or with nested options separated by semicolons:
  $expand=Items($select=Material,Quantity;$filter=Quantity gt 5;$orderby=Quantity desc;$top=3),Customer
  Nested paths are written by nesting: $expand=Items($expand=Product). Never Items/Product in $expand.
- $search=word  free-text search, only when the question asks for a keyword search.""",
    "v2": """\
OData V2 syntax, SAP Gateway compatible (use exactly this; V4 forms such as contains, any/all, nested $expand
options, $count=true or unprefixed dates are invalid here)
- $filter operators: eq ne gt ge lt le, and or not, add sub mul div mod, parentheses for grouping.
- Strings in single quotes, a quote inside a string is doubled: 'O''Neil'. Names are case-sensitive.
- String functions: substringof('abc',Name) (the searched text comes FIRST), startswith(Name,'abc'),
  endswith(Name,'abc'), tolower(Name) eq 'abc', toupper, trim, length(Name) gt 3, indexof, substring, replace, concat.
- Date/time functions: year(SalesOrderDate) eq 2024, month, day, hour, minute, second.
- Math: round, floor, ceiling.
- Literals: Edm.DateTime datetime'2024-01-31T00:00:00' (always with the time part, also for date-only fields) |
  Edm.DateTimeOffset datetimeoffset'2024-01-31T10:00:00Z' | Edm.Time time'PT13H20M' |
  Edm.Guid guid'01234567-89ab-cdef-0123-456789abcdef' | numbers 42 / 42.5 | true false null.
  Codes stored as Edm.String (SAP document numbers, status codes) are quoted: SalesOrder eq '0000012345'.
- Single-valued navigation: to_Customer/Country eq 'DE'. Filtering on a to-many navigation is NOT possible in V2:
  query the item entity set instead, or $expand it.
- $select=SalesOrder,TotalNetAmount  and for expanded data $select=SalesOrder,to_Item/Material
- $orderby=TotalNetAmount desc,SalesOrderDate asc
- $top=10  $skip=20  (integers)
- $inlinecount=allpages  adds the total count; for "how many" questions use $inlinecount=allpages with $top=0.
- $expand=to_Item,to_Item/to_Material  (comma-separated navigation paths, no options in parentheses)
- search=word  SAP free-text search (no $), only on entity sets marked searchable and only for keyword searches.""",
}
