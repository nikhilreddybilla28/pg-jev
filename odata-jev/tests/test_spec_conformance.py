"""Validator and parser against the OData specs.

Sources: the OData ABNF construction rules 4.01/4.0 (oasis-tcs/odata-abnf, odata-abnf-construction-rules.txt),
the operator precedence table of OData 4.01 Part 2 URL Conventions (oasis-tcs/odata-specs), and the OData V2 URI
conventions. Each gap found when reviewing the validator against them has at least one case here.
"""

import pytest

from odata_jev import expression as ex
from odata_jev.metadata import load_tools
from odata_jev.validator import validate

from .conftest import FIXTURES

V4 = load_tools(FIXTURES / "sales_v4.xml")
V2 = load_tools(FIXTURES / "sales_v2.xml")


def errors(v):
    return [str(i) for i in v.issues if i.severity == "error"]


def v4(entity_set="SalesOrders", **opts):
    return validate(V4, entity_set, opts, "v4")


def v2(entity_set="A_Customer", **opts):
    return validate(V2, entity_set, opts, "v2")


def has_error(v, message):
    return any(message in e for e in errors(v))


def tree(text):
    """Compact s-expression of a parse tree, to assert precedence."""

    def s(n):
        if isinstance(n, ex.Binary):
            return f"({n.op} {s(n.left)} {s(n.right)})"
        if isinstance(n, ex.Unary):
            return f"({n.op} {s(n.operand)})"
        if isinstance(n, ex.Literal):
            return n.raw
        if isinstance(n, ex.Path):
            return n.text
        if isinstance(n, ex.ListExpr):
            return "[" + " ".join(s(i) for i in n.items) + "]"
        return type(n).__name__

    return s(ex.parse(text))


# ------------------------------------------------------------------------------------------------ precedence


@pytest.mark.parametrize(
    "text, expected",
    [
        # relational binds tighter than equality; operators of one level associate to the left
        ("A gt 5 eq true", "(eq (gt A 5) true)"),
        ("A eq B ne C", "(ne (eq A B) C)"),
        ("A add B mul C sub D", "(sub (add A (mul B C)) D)"),
        ("A eq 1 or B eq 2 and C eq 3", "(or (eq A 1) (and (eq B 2) (eq C 3)))"),
        # not and unary minus bind tighter than comparisons
        ("not A eq B", "(eq (not A) B)"),
        ("-A lt 0", "(lt (- A) 0)"),
        # has and in are primary operators, so they bind tighter than not
        ("not S has ns.E'X'", "(not (has S ns.E'X'))"),
        ("not C in ('DE','FR')", "(not (in C ['DE' 'FR']))"),
        ("C in ('DE') and B", "(and (in C ['DE']) B)"),
    ],
)
def test_operator_precedence(text, expected):
    assert tree(text) == expected


def test_comparison_chain_validates():
    v = v4(**{"$filter": "NetAmount gt 5 eq true"})
    assert v.valid, errors(v)


@pytest.mark.parametrize(
    "text, message",
    [
        ("Currency EQ 'EUR'", "write the operator 'EQ' in lowercase: 'eq'"),  # 4.0 services are case-sensitive
        ("Items/ANY(i: i/Quantity gt 1)", "write the lambda operator in lowercase: any(...)"),
        ("Currency eq NULL", "write null in lowercase"),  # null = %s"null"
        ("Items/all()", "all() needs a variable and a condition"),  # allExpr has no empty form
    ],
)
def test_case_and_lambda_syntax_errors(text, message):
    assert has_error(v4(**{"$filter": text}), message)


# ------------------------------------------------------------------------------------------------ literals


@pytest.mark.parametrize("literal, value", [("'O''Neil'", "O'Neil"), ("''", ""), ("''''", "'")])
def test_string_literal_escaping(literal, value):
    node = ex.parse(literal)
    assert node.kind == "string" and node.value == value  # SQUOTE-in-string = SQUOTE SQUOTE
    assert v4("Customers", **{"$filter": f"Name eq {literal}"}).valid


def test_backslash_is_not_an_escape():
    assert not v4("Customers", **{"$filter": r"Name eq 'O\'Neil'"}).valid


def test_numeric_literals_nan_inf_and_sign():
    v = v4(**{"$filter": "NetAmount gt -INF and NetAmount ne NaN and NetAmount gt +5"})
    assert v.valid, errors(v)
    assert ex.parse("INFO").segments == ("INFO",)  # a property name that starts like INF is still a name
    assert has_error(v2("A_SalesOrder", **{"$filter": "TotalNetAmount gt -INF"}), "V2 has no NaN/INF literals")


@pytest.mark.parametrize(
    "text",
    [
        "Status eq sales.OrderStatus'Open'",  # schema alias resolves to com.example.sales
        "Status eq com.example.sales.OrderStatus'2'",  # numeric member value (int64Literal)
        "Status has com.example.sales.OrderStatus'Open,Completed'",  # flag list
        "Status has 'Open'",  # 4.01: the qualified type name is optional
        "not Status has sales.OrderStatus'Cancelled'",
    ],
)
def test_enum_literals_valid(text):
    v = v4(**{"$filter": text})
    assert v.valid, errors(v)


@pytest.mark.parametrize(
    "text, message",
    [
        ("Status eq com.example.sales.Other'Open'", "is a com.example.sales.Other literal but Status is"),
        ("Currency eq com.example.sales.OrderStatus'Open'", "is an enumeration literal but Currency"),
        ("Currency has com.example.sales.OrderStatus'Open'", "'has' needs an enumeration property on the left"),
        ("Status has 3", "'has' needs an enumeration literal on the right"),  # hasExpr = "has" enumLiteral
        ("Status has sales.OrderStatus'Open,Shipped'", "'Shipped' is not a member of Status"),
    ],
)
def test_enum_literals_invalid(text, message):
    assert has_error(v4(**{"$filter": text}), message)


def test_guid_literals():
    assert v4("Customers", **{"$filter": "ExternalID eq 01234567-89AB-cdef-0123-456789abcdef"}).valid
    assert not v4("Customers", **{"$filter": "ExternalID eq 01234567-89ab-cdef-0123-456789abcde"}).valid


# ------------------------------------------------------------------------------------------------ lambdas


@pytest.mark.parametrize(
    "text",
    [
        "Items/any()",
        "Items/all(i: i/Quantity gt 0)",
        "Customer/Orders/any(o: o/Items/any(i: i/Material eq 'X'))",
        "Items/any(i: i/NetAmount gt $it/NetAmount)",
        "Tags/any(t: t eq 'vip')",
        "Items/$count gt 2",
    ],
)
def test_lambda_valid(text):
    v = v4(**{"$filter": text})
    assert v.valid, errors(v)


@pytest.mark.parametrize(
    "text, message",
    [
        ("Items/any(i: IsUrgent)", "must use its variable i"),  # lambdaPredicateExpr uses the variable
        ("Customer/any(c: c/Country eq 'DE')", "needs a collection, but Customer is single-valued"),
        ("Items/any(i: i/Quantity)", "must be a condition"),
    ],
)
def test_lambda_invalid(text, message):
    assert has_error(v4(**{"$filter": text}), message)


# ------------------------------------------------------------------------------------------------ dates, functions


@pytest.mark.parametrize(
    "text",
    [
        "OrderDate ge 2024-01-31",
        "CreatedAt ge 2024-01-31T10:00Z",  # seconds are optional
        "CreatedAt ge 2024-01-31T10:00:00.1234567+05:30",  # fractional seconds, numeric offset
        "hour(CreatedAt) eq 10 and year(OrderDate) eq 2024",
        "date(CreatedAt) eq 2024-01-31",
        "CreatedAt ge now() sub duration'P30D'",
        "CreatedAt sub 2024-01-31T00:00:00Z gt duration'P1D'",  # DateTimeOffset - DateTimeOffset = Duration
        "OrderDate add duration'P1D' le 2024-12-31",
        "isof(com.example.sales.SalesOrder)",  # a qualified type name, not a property
        "cast(NetAmount, Edm.String) eq '5'",
        "Customer eq null and ShipTo ne null",  # single-valued navigation and complex values compare to null
    ],
)
def test_v4_dates_functions_and_null(text):
    v = v4(**{"$filter": text})
    assert v.valid, errors(v)


@pytest.mark.parametrize(
    "text, message",
    [
        ("CreatedAt ge 2024-01-31T25:00:00Z", "not a valid date"),
        ("OrderDate ge 2024-13-01", "not a valid date"),
        ("hour(OrderDate) eq 10", "hour() needs a date-time or time-of-day argument"),  # hour(Edm.Date) undefined
        ("year(13:00:00) eq 2024", "year() needs a date argument"),
        ("OrderDate add 1 gt 2024-01-01", "'add' is not defined"),
        ("Items eq null", "cannot be compared"),  # collections never compare
    ],
)
def test_v4_dates_invalid(text, message):
    assert has_error(v4(**{"$filter": text}), message)


def test_timeofday_and_unprefixed_duration():
    svc = load_tools(
        {
            "entity_sets": [
                {
                    "name": "Shifts",
                    "keys": ["ID"],
                    "properties": [
                        {"name": "ID"},
                        {"name": "Start", "type": "Edm.TimeOfDay"},
                        {"name": "Length", "type": "Edm.Duration"},
                    ],
                }
            ]
        }
    )
    ok = validate(svc, "Shifts", {"$filter": "Start ge 08:30 and Length le 'PT8H'"}, "v4")  # 4.01: prefix optional
    assert ok.valid, errors(ok)
    assert validate(svc, "Shifts", {"$filter": "Length le duration'PT8H'"}, "v4").valid
    assert has_error(validate(svc, "Shifts", {"$filter": "Start ge 24:30:00"}, "v4"), "not a valid time of day")


@pytest.mark.parametrize(
    "text, message",
    [
        ("contains('Acme', Name)", "contains takes the property first: contains(Name,'Acme')"),
        ("startswith('A', Name)", "startswith takes the property first: startswith(Name,'A')"),
        ("contains(Name, 5)", "contains() compares strings"),
    ],
)
def test_string_function_arguments(text, message):
    assert has_error(v4("Customers", **{"$filter": text}), message)


def test_in_operator_operands():
    assert v4(**{"$filter": "'vip' in Tags"}).valid  # inExpr: a single expression resolving to a collection
    assert has_error(v4("Customers", **{"$filter": "Country in ('DE', Name)"}), "takes literal values only")
    assert has_error(v4("Customers", **{"$filter": "Country in Name"}), "'in' needs a list (a, b) or a collection")


def test_matches_pattern_spellings():
    for name in ("matchesPattern", "matchespattern"):  # 4.01 function names are case-insensitive
        v = v4("Customers", **{"$filter": f"{name}(Name,'^A.*e$')"})
        assert v.valid, errors(v)


# ------------------------------------------------------------------------------------------------ $expand


@pytest.mark.parametrize(
    "expand",
    [
        "*",
        "*($levels=2)",
        "Items/$ref",
        "Items/$ref($filter=Quantity gt 1;$top=2)",
        "Items/$count($filter=Quantity gt 1)",
        "Items($filter=Quantity gt 1 and contains(Material,'a;b');$select=Material,Quantity;"
        "$orderby=Quantity desc;$top=5;$skip=1;$count=true)",
        "Items($filter=Material eq 'a)b')",
        "Items($expand=Order($select=SalesOrderID;$expand=Customer($select=Name)))",
        "Items($select=Material),Customer($select=Name)",
        "Customer($levels=max)",
        "Items(filter=Quantity gt 1;select=Material)",  # 4.01: option names without $
    ],
)
def test_v4_expand_valid(expand):
    v = v4(**{"$expand": expand})
    assert v.valid, errors(v)


@pytest.mark.parametrize(
    "expand, message",
    [
        ("Items($filter=Bogus eq 1)", "unknown property 'Bogus'"),
        ("Items($select=Order/SalesOrderID)", "$expand=Order($select=...)"),
        ("Items($filter=Items/any(x: x/Quantity gt 1))", "unknown property 'Items'"),  # relative to the target
        ("Items($top=x)", "must be a non-negative integer"),
        ("Items($levels=0)", "$levels must be a positive integer or max"),  # oneToNine *DIGIT / "max"
        ("Items/$count($select=Material)", "$select is not allowed here"),  # expandCountOption
        ("Items/$ref($expand=Order)", "$expand is not allowed here"),  # expandRefOption
        ("*($top=2)", "only $levels may follow *"),
        ("Items($filter=Quantity gt 1", "malformed item"),
        ("Items($format=json)", "$format is not allowed here"),
    ],
)
def test_v4_expand_invalid(expand, message):
    assert has_error(v4(**{"$expand": expand}), message)


# ------------------------------------------------------------------------------------------------ V2


@pytest.mark.parametrize(
    "text",
    [
        "substringof('Acme',CustomerName) eq true",
        "substringof('acme',tolower(CustomerName))",
        "not substringof('x',CustomerName)",
        "startswith(CustomerName,'A') eq true",
        "endswith(CustomerName,'GmbH')",
        "CreationDate ge datetime'2024-01-31T00:00'",  # seconds optional
        "CreationDate ge datetime'2024-01-31T00:00:00.1234567'",  # up to 7 fractional digits
        "CustomerName eq 'O''Neil'",
    ],
)
def test_v2_functions_and_literals_valid(text):
    v = v2(**{"$filter": text})
    assert v.valid, errors(v)


@pytest.mark.parametrize(
    "text, message",
    [
        ("startswith('A',CustomerName)", "startswith takes the property first"),
        ("substringof('Acme')", "substringof() takes 2 arguments"),
        ("CreationDate ge datetime'2024-01-31T00:00:00Z'", "datetime'…' has no time zone"),
        ("CreationDate ge datetime'2024-02-30T00:00:00'", "not a valid date"),
        ("CreationDate ge datetimeoffset'2024-01-31T00:00:00Z'", "compare it with datetime'…'"),
        ("CreationDate ge 2024-01-31T00:00:00Z", "V2 date-time literals are prefixed"),
        ("contains(CustomerName,'A')", "V2 has no contains()"),
    ],
)
def test_v2_functions_and_literals_invalid(text, message):
    assert has_error(v2(**{"$filter": text}), message)


def test_v2_datetimeoffset_property_needs_datetimeoffset_literal():
    ok = v2("A_SalesOrder", **{"$filter": "LastChangeDateTime ge datetimeoffset'2024-01-31T00:00:00Z'"})
    assert ok.valid, errors(ok)
    bad = v2("A_SalesOrder", **{"$filter": "LastChangeDateTime ge datetime'2024-01-31T00:00:00'"})
    assert has_error(bad, "compare it with datetimeoffset'2024-01-31T00:00:00Z'")


def test_v2_inlinecount_values():
    assert v2("A_SalesOrder", **{"$inlinecount": "allpages"}).query_options == {"$inlinecount": "allpages"}
    assert v2("A_SalesOrder", **{"$inlinecount": "none"}).query_options == {}
    assert has_error(v2("A_SalesOrder", **{"$inlinecount": "some"}), "must be allpages or none")


@pytest.mark.parametrize(
    "text, message",
    [
        ("to_Item/any(i: i/Material eq 'X')", "is V4 only"),
        ("to_Item/all(i: i/Material eq 'X')", "is V4 only"),
        ("to_Item/any()", "is V4 only"),
        ("to_Item/$count gt 0", "V4 only"),
        ("SalesOrganization in ('1000','2000')", "'in' is V4 only"),
    ],
)
def test_v2_has_no_lambdas_count_or_in(text, message):
    assert has_error(v2("A_SalesOrder", **{"$filter": text}), message)


def test_v2_single_navigation_compares_to_null():
    assert v2("A_SalesOrder", **{"$filter": "to_SoldToParty eq null"}).valid
