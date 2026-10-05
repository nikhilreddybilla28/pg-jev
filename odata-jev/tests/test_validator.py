import pytest

from odata_jev import expression as ex
from odata_jev.metadata import load_tools
from odata_jev.validator import validate

from .conftest import FIXTURES

V4 = load_tools(FIXTURES / "sales_v4.xml")
V2 = load_tools(FIXTURES / "sales_v2.xml")
TOOLS = load_tools(FIXTURES / "sales_tools.json")


def errors(v):
    return [str(i) for i in v.issues if i.severity == "error"]


def check(service, entity_set, version, **opts):
    return validate(service, entity_set, opts, version)


# ------------------------------------------------------------------------------------------------ parser


def test_parser_precedence_and_nodes():
    node = ex.parse("not startswith(Name,'A') and NetAmount add 5 gt 10 or Status eq sales.OrderStatus'Open'")
    assert isinstance(node, ex.Binary) and node.op == "or"
    left = node.left
    assert left.op == "and" and isinstance(left.left, ex.Unary) and left.left.op == "not"
    cmp = left.right
    assert cmp.op == "gt" and cmp.left.op == "add"
    assert node.right.right.kind == "enum" and node.right.right.value == "Open"


@pytest.mark.parametrize(
    "text, kind",
    [
        ("'O''Neil'", "string"),
        ("datetime'2024-01-31T00:00:00'", "datetime_v2"),
        ("datetimeoffset'2024-01-31T00:00:00Z'", "datetimeoffset_v2"),
        ("guid'01234567-89ab-cdef-0123-456789abcdef'", "guid_v2"),
        ("01234567-89ab-cdef-0123-456789abcdef", "guid"),
        ("2024-01-31", "date"),
        ("2024-01-31T10:00:00Z", "datetimeoffset"),
        ("2024-01-31T10:00:00+02:00", "datetimeoffset"),
        ("2024-01-31T10:00:00", "datetime_no_offset"),
        ("10:30:00", "timeofday"),
        ("42", "int"),
        ("-4.5", "decimal"),
        ("4.5M", "decimal_suffix"),
        ("7L", "int64_suffix"),
        ("1e10", "double"),
        ("duration'P1D'", "duration"),
        ("null", "null"),
        ("true", "bool"),
    ],
)
def test_literal_kinds(text, kind):
    node = ex.parse(text)
    assert isinstance(node, ex.Literal) and node.kind == kind
    if kind == "string":
        assert node.value == "O'Neil"


def test_parser_lambda_in_and_errors():
    lam = ex.parse("Items/any(i: i/Quantity gt 5)")
    assert isinstance(lam, ex.Lambda) and lam.var == "i" and lam.path.segments == ("Items",)
    node = ex.parse("Country in ('DE','FR')")
    assert node.op == "in" and len(node.right.items) == 2
    with pytest.raises(ex.ParseError, match="unterminated"):
        ex.parse("Name eq 'O'Neil'")
    with pytest.raises(ex.ParseError):
        ex.parse("Name eq")
    assert ex.split_top_level("a,b(c,d),'x,y'", ",") == ["a", "b(c,d)", "'x,y'"]


# ------------------------------------------------------------------------------------------------ properties


def test_rejects_invented_property_with_suggestion():
    v = check(TOOLS, "SalesOrder", "v2", **{"$filter": "CustomerName eq 'ACME'", "$select": "SalesOrder,NetValue"})
    assert not v.valid
    errs = errors(v)
    assert any("unknown property 'CustomerName' on SalesOrder" in e for e in errs)
    assert any("unknown property 'NetValue'" in e for e in errs)


def test_case_mismatch_suggests_exact_name():
    v = check(TOOLS, "SalesOrder", "v2", **{"$orderby": "totalnetamount desc"})
    assert "did you mean 'TotalNetAmount'? names are case-sensitive" in errors(v)[0]


def test_unknown_entity_set_and_navigation():
    v = check(TOOLS, "SalesOrders", "v2", **{"$top": 5})
    assert "unknown entity set 'SalesOrders'" in errors(v)[0] and "'SalesOrder'" in errors(v)[0]
    v = check(TOOLS, "SalesOrder", "v2", **{"$expand": "to_Items"})
    assert "did you mean 'to_Item'" in errors(v)[0]


def test_valid_query_reports_referenced_fields():
    v = check(
        TOOLS,
        "SalesOrder",
        "v2",
        **{
            "$filter": "to_Customer/Country eq 'DE' and TotalNetAmount gt 1000",
            "$select": "SalesOrder,TotalNetAmount,to_Item/Material",
            "$expand": "to_Item",
            "$orderby": "TotalNetAmount desc",
            "$top": "10",
        },
    )
    assert v.valid, errors(v)
    assert v.query_options["$top"] == 10
    assert list(v.query_options) == ["$filter", "$select", "$expand", "$orderby", "$top"]
    assert {"to_Customer/Country", "TotalNetAmount", "to_Item/Material", "SalesOrder"} <= set(v.fields)


# ------------------------------------------------------------------------------------------------ V2 vs V4


def test_v2_and_v4_string_functions():
    assert check(TOOLS, "Customer", "v2", **{"$filter": "substringof('Acme',CustomerName)"}).valid
    assert check(V4, "Customers", "v4", **{"$filter": "contains(Name,'Acme')"}).valid
    v = check(TOOLS, "Customer", "v2", **{"$filter": "contains(CustomerName,'Acme')"})
    assert "V2 has no contains(); use substringof('text',Property)" in errors(v)[0]
    v = check(V4, "Customers", "v4", **{"$filter": "substringof('Acme',Name)"})
    assert "V4 has no substringof(); use contains(Property,'text')" in errors(v)[0]
    v = check(TOOLS, "Customer", "v2", **{"$filter": "substringof(CustomerName,'Acme')"})
    assert "substringof takes the searched text first: substringof('Acme',CustomerName)" in errors(v)[0]


def test_v2_and_v4_date_literals():
    ok_v2 = check(V2, "A_SalesOrder", "v2", **{"$filter": "SalesOrderDate ge datetime'2024-01-01T00:00:00'"})
    assert ok_v2.valid, errors(ok_v2)
    ok_v4 = check(
        V4, "SalesOrders", "v4", **{"$filter": "OrderDate ge 2024-01-01 and CreatedAt lt 2024-02-01T00:00:00Z"}
    )
    assert ok_v4.valid, errors(ok_v4)

    v = check(V2, "A_SalesOrder", "v2", **{"$filter": "SalesOrderDate ge datetime'2024-01-01'"})
    assert "needs the time part: datetime'2024-01-01T00:00:00'" in errors(v)[0]
    v = check(V2, "A_SalesOrder", "v2", **{"$filter": "SalesOrderDate ge 2024-01-01"})
    assert "V2 has no date literal" in errors(v)[0]
    v = check(V4, "SalesOrders", "v4", **{"$filter": "OrderDate ge datetime'2024-01-01T00:00:00'"})
    assert "V4 has no datetime'…' literal" in errors(v)[0]
    v = check(V4, "SalesOrders", "v4", **{"$filter": "CreatedAt ge 2024-01-01T00:00:00"})
    assert "need a time zone" in errors(v)[0]
    v = check(V4, "SalesOrders", "v4", **{"$filter": "CreatedAt ge 2024-01-01"})
    assert "use date(Property) eq 2024-01-31" in errors(v)[0]
    v = check(V4, "SalesOrders", "v4", **{"$filter": "OrderDate eq 2024-02-30"})
    assert "not a valid date" in errors(v)[0]


def test_v2_and_v4_guid_literals():
    g = "01234567-89ab-cdef-0123-456789abcdef"
    assert check(V2, "A_Customer", "v2", **{"$filter": f"CustomerUUID eq guid'{g}'"}).valid
    assert check(V4, "Customers", "v4", **{"$filter": f"ExternalID eq {g}"}).valid
    assert (
        "V2 GUID literals are prefixed"
        in errors(check(V2, "A_Customer", "v2", **{"$filter": f"CustomerUUID eq {g}"}))[0]
    )
    v = check(V4, "Customers", "v4", **{"$filter": f"ExternalID eq '{g}'"})
    assert "write the value like 01234567-" in errors(v)[0]


def test_count_option_is_mapped_to_the_version():
    v2 = check(TOOLS, "SalesOrder", "v2", **{"$count": True, "$top": 0})
    assert v2.valid and v2.query_options == {"$top": 0, "$inlinecount": "allpages"}
    assert any(i.severity == "fixed" and "$inlinecount=allpages" in i.message for i in v2.issues)
    v4 = check(V4, "SalesOrders", "v4", **{"$inlinecount": "allpages"})
    assert v4.valid and v4.query_options == {"$count": True}


def test_lambda_only_in_v4():
    ok = check(V4, "SalesOrders", "v4", **{"$filter": "Items/any(i: i/Material eq 'M-01' and i/Quantity gt 5)"})
    assert ok.valid, errors(ok)
    assert "Items/Material" in ok.fields
    v = check(TOOLS, "SalesOrder", "v2", **{"$filter": "to_Item/any(i: i/Material eq 'M-01')"})
    assert "is V4 only" in errors(v)[0]
    v = check(TOOLS, "SalesOrder", "v2", **{"$filter": "to_Item/Material eq 'M-01'"})
    assert "query SalesOrderItem directly" in errors(v)[0]
    v = check(V4, "SalesOrders", "v4", **{"$filter": "Items/Material eq 'M-01'"})
    assert "Items/any(x: x/... eq ...)" in errors(v)[0]


def test_expand_syntax_differs():
    ok4 = check(
        V4,
        "SalesOrders",
        "v4",
        **{"$expand": "Items($select=Material,Quantity;$filter=Quantity gt 5;$orderby=Quantity desc;$top=3),Customer"},
    )
    assert ok4.valid, errors(ok4)
    v = check(V4, "SalesOrders", "v4", **{"$expand": "Items($select=Nope)"})
    assert "unknown property 'Nope'" in errors(v)[0]
    v = check(V4, "SalesOrderItems", "v4", **{"$expand": "Order/Customer"})
    assert "V4 nests expansions: $expand=Order($expand=Customer)" in errors(v)[0]
    v = check(TOOLS, "SalesOrder", "v2", **{"$expand": "to_Item($select=Material)"})
    assert "V2 has no options inside $expand" in errors(v)[0]
    ok2 = check(TOOLS, "SalesOrderItem", "v2", **{"$expand": "to_SalesOrder/to_Customer"})
    assert ok2.valid, errors(ok2)


def test_v2_select_through_navigation_needs_expand():
    v = check(TOOLS, "SalesOrder", "v2", **{"$select": "SalesOrder,to_Item/Material"})
    assert "add $expand=to_Item" in errors(v)[0]
    v = check(V4, "SalesOrders", "v4", **{"$select": "SalesOrderID,Items/Material", "$expand": "Items"})
    assert "$expand=Items($select=...)" in errors(v)[0]
    assert check(V4, "SalesOrders", "v4", **{"$select": "SalesOrderID,ShipTo/City"}).valid


def test_search_option():
    assert check(TOOLS, "SalesOrder", "v2", **{"search": "pump"}).valid
    v = check(TOOLS, "SalesOrder", "v2", **{"$search": "pump"})
    assert v.valid and v.query_options == {"search": "pump"}
    assert "not marked searchable" in errors(check(TOOLS, "Customer", "v2", **{"search": "x"}))[0]
    assert check(V4, "SalesOrders", "v4", **{"$search": "pump"}).valid
    assert "does not support $search" in errors(check(V4, "SalesOrderItems", "v4", **{"$search": "x"}))[0]


# ------------------------------------------------------------------------------------------------ types


def test_literal_types_must_match_properties():
    v = check(TOOLS, "SalesOrder", "v2", **{"$filter": "SalesOrder eq 12345"})
    assert "SalesOrder is Edm.String but 12345 is a numeric literal; write the value like 'text'" in errors(v)[0]
    v = check(TOOLS, "SalesOrder", "v2", **{"$filter": "TotalNetAmount gt '1000'"})
    assert "TotalNetAmount is Edm.Decimal but '1000' is a string literal" in errors(v)[0]
    v = check(V4, "SalesOrders", "v4", **{"$filter": "NetAmount gt 4.5M"})
    assert "V4 numbers take no M suffix" in errors(v)[0]
    assert check(TOOLS, "SalesOrder", "v2", **{"$filter": "TotalNetAmount gt 4.5M"}).valid
    v = check(TOOLS, "SalesOrder", "v2", **{"$filter": "TotalNetAmount"})
    assert "must be a condition" in errors(v)[0]
    v = check(TOOLS, "SalesOrder", "v2", **{"$filter": "length(TotalNetAmount) gt 3"})
    assert "length() needs a string argument" in errors(v)[0]
    v = check(TOOLS, "SalesOrder", "v2", **{"$filter": "TotalNetAmount gt null"})
    assert "'gt null' is never true" in errors(v)[0]


def test_closed_code_lists():
    v = check(TOOLS, "SalesOrder", "v2", **{"$filter": "OverallSDProcessStatus eq 'Open'"})
    assert (
        "'Open' is not a valid value of OverallSDProcessStatus; use one of: 'A' (Not yet processed (open))"
        in errors(v)[0]
    )
    assert check(TOOLS, "SalesOrder", "v2", **{"$filter": "OverallSDProcessStatus eq 'A'"}).valid
    assert check(V4, "SalesOrders", "v4", **{"$filter": "Status eq com.example.sales.OrderStatus'Open'"}).valid
    assert check(V4, "SalesOrders", "v4", **{"$filter": "Status eq 'Completed'"}).valid
    v = check(V4, "SalesOrders", "v4", **{"$filter": "Status eq 'Shipped'"})
    assert "'Shipped' is not a member of Status" in errors(v)[0]


def test_functions_and_operators():
    assert check(
        TOOLS, "SalesOrder", "v2", **{"$filter": "year(SalesOrderDate) eq 2024 and month(SalesOrderDate) le 6"}
    ).valid
    v = check(TOOLS, "SalesOrder", "v2", **{"$filter": "now() gt SalesOrderDate"})
    assert "now() is not available in OData V2" in errors(v)[0]
    v = check(TOOLS, "SalesOrder", "v2", **{"$filter": "startswith(SoldToParty)"})
    assert "startswith() takes 2 arguments, got 1" in errors(v)[0]
    v = check(TOOLS, "SalesOrder", "v2", **{"$filter": "frobnicate(SoldToParty)"})
    assert "unknown function frobnicate()" in errors(v)[0]
    v4_in = check(V4, "Customers", "v4", **{"$filter": "Country in ('DE','FR')"})
    assert v4_in.valid and any("4.01" in i.message for i in v4_in.issues)
    assert "'in' is V4 only" in errors(check(TOOLS, "Customer", "v2", **{"$filter": "Country in ('DE','FR')"}))[0]
    v = check(TOOLS, "Customer", "v2", **{"$filter": "CustomerName eq 'O'Neil'"})
    assert "cannot parse" in errors(v)[0] and "quote inside a string" in errors(v)[0]


# ------------------------------------------------------------------------------------------------ options + SAP flags


def test_option_normalisation():
    v = check(
        TOOLS,
        "SalesOrder",
        "v2",
        **{"filter": "SoldToParty eq '1'", "select": ["SalesOrder", "SoldToParty"], "$Top": "5", "$skip": None},
    )
    assert v.valid, errors(v)
    assert v.query_options == {"$filter": "SoldToParty eq '1'", "$select": "SalesOrder,SoldToParty", "$top": 5}
    assert sum(i.severity == "fixed" for i in v.issues) >= 4
    assert "non-negative integer" in errors(check(TOOLS, "SalesOrder", "v2", **{"$top": -1}))[0]
    assert "non-negative integer" in errors(check(TOOLS, "SalesOrder", "v2", **{"$top": "ten"}))[0]
    assert "unsupported query option" in errors(check(V4, "SalesOrders", "v4", **{"$apply": "groupby((Currency))"}))[0]


def test_sap_capability_flags():
    v = check(TOOLS, "SalesOrder", "v2", **{"$filter": "HeaderBillingBlockReason eq '01'"})
    assert "HeaderBillingBlockReason is not filterable" in errors(v)[0]
    v = check(TOOLS, "SalesOrder", "v2", **{"$orderby": "HeaderBillingBlockReason"})
    assert "not sortable" in errors(v)[0]
    v = check(V4, "SalesOrders", "v4", **{"$filter": "contains(Note,'late')"})
    assert "Note is not filterable" in errors(v)[0]
    v = check(V2, "A_SalesOrderText", "v2", **{"$top": 5})
    assert any("addressable" in e for e in errors(v)) and any("paging" in e for e in errors(v))


def test_required_filter():
    svc = load_tools(
        {
            "entity_sets": [
                {
                    "name": "Stock",
                    "keys": ["Plant"],
                    "properties": [{"name": "Plant", "required_in_filter": True}, {"name": "Material"}],
                }
            ]
        }
    )
    assert "requires a $filter on Plant" in errors(validate(svc, "Stock", {}, "v4"))[0]
    v = validate(svc, "Stock", {"$filter": "Material eq 'X'"}, "v4")
    assert "requires a filter on Plant" in errors(v)[0]
    assert validate(svc, "Stock", {"$filter": "Plant eq '1000'"}, "v4").valid


def test_not_binds_tighter_than_comparisons():
    node = ex.parse("not Country eq 'DE'")
    assert node.op == "eq" and isinstance(node.left, ex.Unary)  # (not Country) eq 'DE', as servers parse it
    v = check(TOOLS, "Customer", "v2", **{"$filter": "not Country eq 'DE'"})
    assert "'not' binds tighter than comparisons" in errors(v)[0]
    assert check(TOOLS, "Customer", "v2", **{"$filter": "not (Country eq 'DE')"}).valid
    assert check(TOOLS, "Customer", "v2", **{"$filter": "not substringof('x',CustomerName) and Country eq 'DE'"}).valid
