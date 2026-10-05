from urllib.parse import parse_qsl, unquote, urlsplit

from odata_jev.builder import build_query


def test_order_encoding_and_parts():
    b = build_query(
        "SalesOrder",
        {
            "$top": 10,
            "$filter": "CustomerName eq 'Müller & Söhne' and TotalNetAmount gt 1000",
            "$inlinecount": "allpages",
            "$select": "SalesOrder,TotalNetAmount",
            "$orderby": "TotalNetAmount desc",
        },
        "https://host/sap/opu/odata/sap/API_SALES_ORDER_SRV/",
    )
    assert list(b.parts) == ["$filter", "$select", "$orderby", "$top", "$inlinecount"]
    assert b.url.startswith("https://host/sap/opu/odata/sap/API_SALES_ORDER_SRV/SalesOrder?$filter=")
    assert "%20" in b.query_string and "+" not in b.query_string
    assert "%26" in b.encoded_parts["$filter"]  # & inside a value never splits the query string
    assert "M%C3%BCller" in b.encoded_parts["$filter"]
    assert "'" in b.encoded_parts["$filter"] and "$select=SalesOrder,TotalNetAmount" in b.query_string
    decoded = dict(parse_qsl(urlsplit(b.url).query))
    assert decoded["$filter"] == "CustomerName eq 'Müller & Söhne' and TotalNetAmount gt 1000"
    assert decoded["$top"] == "10"
    assert b.readable.startswith("SalesOrder?$filter=CustomerName eq 'Müller & Söhne'")


def test_plus_in_offsets_and_v4_values():
    b = build_query(
        "SalesOrders",
        {
            "$filter": "CreatedAt ge 2024-01-01T00:00:00+02:00",
            "$count": True,
            "$expand": "Items($top=3;$select=Material)",
        },
    )
    assert b.url == b.path  # no service URL: relative
    assert "%2B02:00" in b.encoded_parts["$filter"]
    assert b.parts["$count"] == "true"
    assert b.encoded_parts["$expand"] == "Items($top=3;$select=Material)"
    assert unquote(b.encoded_parts["$filter"]) == "CreatedAt ge 2024-01-01T00:00:00+02:00"


def test_no_options():
    b = build_query("Customers", {}, "https://h/svc")
    assert b.url == "https://h/svc/Customers" and b.query_string == "" and b.readable == "Customers"


def test_service_url_with_its_own_query_parameters():
    b = build_query("A_SalesOrder", {"$top": 5}, "https://s4/sap/opu/odata/sap/API_SALES_ORDER_SRV/?sap-client=100")
    assert b.url == "https://s4/sap/opu/odata/sap/API_SALES_ORDER_SRV/A_SalesOrder?sap-client=100&$top=5"
    assert b.path == "A_SalesOrder?$top=5"
    b = build_query("A_SalesOrder", {}, "https://s4/sap/opu/odata/sap/API_SALES_ORDER_SRV?sap-client=100")
    assert b.url == "https://s4/sap/opu/odata/sap/API_SALES_ORDER_SRV/A_SalesOrder?sap-client=100"
