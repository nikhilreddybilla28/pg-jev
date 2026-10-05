import json

import pytest

from odata_jev.errors import MetadataError
from odata_jev.metadata import load_tools, parse_edmx

from .conftest import FIXTURES


def test_v4_edmx_normalises_types_sets_and_annotations():
    svc = load_tools(FIXTURES / "sales_v4.xml")
    assert svc.version == "v4"
    assert [s.name for s in svc.entity_sets] == ["SalesOrders", "SalesOrderItems", "Customers"]

    orders = svc.entity_set("SalesOrders")
    assert orders.keys == ["SalesOrderID"]
    assert orders.display_label() == "Sales Order Header"  # external Annotations on the type, aliased vocabulary
    net = orders.property("NetAmount")
    assert (net.type, net.precision, net.scale) == ("Edm.Decimal", 16, 3)
    assert net.label == "Net Value"
    assert net.description == "Net value of the order in document currency"
    assert orders.property("Note").description.startswith("Free-text note")  # element-form <String>
    assert orders.property("SalesOrderID").label == "Sales Order"  # inline annotation

    status = orders.property("Status")
    assert status.kind == "enum" and list(status.values) == ["Open", "InProcess", "Completed", "Cancelled"]
    assert status.type == "com.example.sales.OrderStatus"  # schema alias "sales" resolved
    assert orders.property("ShipTo").kind == "complex"
    assert orders.property("Tags").collection is True

    assert orders.non_filterable == ["Note"]  # Capabilities.FilterRestrictions
    assert svc.entity_set("SalesOrderItems").searchable is False  # Capabilities.SearchRestrictions
    assert svc.entity_set("Customers").description == "Business partners who place sales orders"

    items = orders.navigation("Items")
    assert items.collection is True and items.target_type == "com.example.sales.SalesOrderItem"
    assert svc.nav_target_set(items, orders).name == "SalesOrderItems"
    assert orders.navigation("Customer").collection is False
    assert svc.resolve(orders.entity_type, "Items/Material").name == "Material"
    assert svc.resolve(orders.entity_type, "ShipTo/City").name == "City"
    assert svc.resolve(orders.entity_type, "ShipTo/Nope") is None


def test_v2_edmx_reads_sap_annotations_and_associations():
    svc = load_tools(FIXTURES / "sales_v2.xml")
    assert svc.version == "v2"
    so = svc.entity_set("A_SalesOrder")
    assert so.searchable is True
    assert so.display_label() == "Sales Order Header"
    date = so.property("SalesOrderDate")
    assert (date.type, date.label, date.display_format) == ("Edm.DateTime", "Document Date", "Date")
    assert date.description == "Document Date (Date Received/Sent)"  # sap:quickinfo
    block = so.property("HeaderBillingBlockReason")
    assert block.filterable is False and block.sortable is False

    to_item = so.navigation("to_Item")
    assert to_item.collection is True
    assert so.nav_bindings == {"to_Item": "A_SalesOrderItem", "to_SoldToParty": "A_Customer"}
    assert so.navigation("to_SoldToParty").collection is False  # Multiplicity 0..1
    item = svc.entity_set("A_SalesOrderItem")
    assert item.navigation("to_SalesOrder").collection is False
    assert item.nav_bindings == {"to_SalesOrder": "A_SalesOrder"}

    text = svc.entity_set("A_SalesOrderText")
    assert text.addressable is False and text.pageable is False
    assert svc.entity_set("A_Customer").property("CustomerUUID").type == "Edm.Guid"


def test_json_tool_details():
    svc = load_tools(FIXTURES / "sales_tools.json")
    assert svc.version == "v2"
    assert svc.service_url.endswith("/API_SALES_ORDER_SRV")
    so = svc.entity_set("SalesOrder")
    assert len(so.properties) == 42
    assert so.entity_type.name == "A_SalesOrderType"
    assert so.property("OverallSDProcessStatus").values["A"].startswith("Not yet processed")
    assert so.nav_bindings == {"to_Item": "SalesOrderItem", "to_Customer": "Customer"}
    assert svc.resolve(so.entity_type, "to_Item/Material").label == "Material"
    assert svc.resolve(so.entity_type, "to_Customer/Country").max_length == 3


def test_same_service_from_json_string_mapping_and_path():
    raw = (FIXTURES / "sales_tools.json").read_text()
    a = load_tools(raw)
    b = load_tools(json.loads(raw))
    c = load_tools(str(FIXTURES / "sales_tools.json"), service_url="https://override")
    assert [s.name for s in a.entity_sets] == [s.name for s in b.entity_sets] == [s.name for s in c.entity_sets]
    assert c.service_url == "https://override"


def test_json_minimal_with_list_values_and_multiplicity():
    svc = load_tools(
        {
            "entity_sets": [
                {
                    "name": "Orders",
                    "keys": ["ID"],
                    "properties": [
                        {"name": "ID", "type": "Edm.Int32"},
                        {"name": "Status", "values": ["open", "closed"]},
                    ],
                    "navigation_properties": [{"name": "Lines", "target": "Lines", "multiplicity": "*"}],
                },
                {"name": "Lines", "keys": ["ID"], "properties": [{"name": "ID", "type": "Edm.Int32"}]},
            ]
        }
    )
    orders = svc.entity_set("Orders")
    assert svc.version is None
    assert orders.property("Status").values == {"open": None, "closed": None}
    assert orders.navigation("Lines").collection is True


@pytest.mark.parametrize(
    "doc, message",
    [
        ({"entity_sets": []}, "no entity sets"),
        ({"entity_sets": [{"name": "A", "keys": ["X"], "properties": [{"name": "Y"}]}]}, "key 'X'"),
        ({"entity_sets": [{"name": "A", "properties": [{"name": "Y", "type": "Foo"}]}]}, "unknown type"),
        (
            {"entity_sets": [{"name": "A", "navigation_properties": [{"name": "n", "target": "B"}]}]},
            "unknown entity set",
        ),
        ({"version": "v3.5", "entity_sets": [{"name": "A"}]}, "unknown OData version"),
    ],
)
def test_json_errors(doc, message):
    with pytest.raises(MetadataError, match=message):
        load_tools(doc)


def test_edmx_errors_and_no_external_entities(tmp_path):
    with pytest.raises(MetadataError, match="invalid"):
        parse_edmx("<edmx:Edmx")
    with pytest.raises(MetadataError, match="not an EDMX"):
        parse_edmx("<root/>")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET")
    xxe = f"""<?xml version="1.0"?>
<!DOCTYPE x [<!ENTITY s SYSTEM "file://{secret}">]>
<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx"><edmx:DataServices>
<Schema Namespace="n" xmlns="http://docs.oasis-open.org/odata/ns/edm">
<EntityType Name="T"><Key><PropertyRef Name="K"/></Key><Property Name="K" Type="Edm.String">
<Annotation Term="Core.Description"><String>&s;</String></Annotation></Property></EntityType>
<EntityContainer Name="C"><EntitySet Name="S" EntityType="n.T"/></EntityContainer>
</Schema></edmx:DataServices></edmx:Edmx>"""
    svc = parse_edmx(xxe)
    assert "TOPSECRET" not in (svc.entity_set("S").property("K").description or "")
    with pytest.raises(MetadataError, match="external entity"):  # in an attribute, lxml refuses outright
        parse_edmx(xxe.replace("<String>&s;</String></Annotation>", '<String x="&s;"/></Annotation>'))
