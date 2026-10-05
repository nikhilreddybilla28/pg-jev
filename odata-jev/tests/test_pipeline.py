import json

import pytest

from odata_jev import NoValidQueryError, Session, extract_records, text_to_odata
from odata_jev.pipeline import FIELD_Q, ROW_Q, SELECT_Q, VERIFY_Q, clean_record

from .conftest import FIXTURES

TOOLS = FIXTURES / "sales_tools.json"
TODAY = "2026-10-05"


def cand(entity_set, rationale="because", residual=None, **opts):
    return {"entity_set": entity_set, "query_options": opts, "residual_condition": residual, "rationale": rationale}


def rules(**by_key):
    """Mock Jev rule dispatching on the items key: entity_sets / fields / candidates / rows."""

    def rule(ctx):
        fn = by_key.get(ctx.items_key)
        return fn(ctx.item) if fn else 0.5

    return rule


def user_prompt(llm, i=0):
    return llm.generation_requests()[i]["messages"][1]["content"]


def system_prompt(llm, i=0):
    return llm.generation_requests()[i]["messages"][0]["content"]


def jev_requests(jev, items_key):
    return [r for r in jev.requests if items_key in r["state"]]


@pytest.fixture
def session(settings):
    with Session(settings) as s:
        yield s


# ------------------------------------------------------------------------------------------------ selection


def test_entity_set_selection_picks_the_top_set(session, jev, llm):
    jev.rule = rules(entity_sets=lambda it: 0.9 if it["name"] == "Customer" else 0.2, candidates=lambda it: 0.88)
    llm.candidates = [
        cand(
            "Customer",
            "Customers filtered by country.",
            **{"$filter": "Country eq 'DE'", "$select": "Customer,CustomerName"},
        )
    ]
    r = session.text_to_odata("Which customers are located in Germany?", TOOLS, today=TODAY)

    assert r.entity_set == "Customer"
    assert (r.entity_sets[0].name, r.entity_sets[0].selected) == ("Customer", True)
    assert sum(s.selected for s in r.entity_sets) == 1
    sel = jev_requests(jev, "entity_sets")
    assert len(sel) == 1 and len(sel[0]["state"]["entity_sets"]) == 3
    assert sel[0]["state"]["question"] == "Which customers are located in Germany?"
    assert sel[0]["questions"]["r2"]["instructions"] == SELECT_Q.format(ref="entity_sets[2]")
    summary = next(s for s in sel[0]["state"]["entity_sets"] if s["name"] == "SalesOrder")
    assert "to_Item -> SalesOrderItem (many)" in summary["navigation"]
    prompt = user_prompt(llm)
    assert "## Customer (Customer)" in prompt and "## SalesOrder" not in prompt
    assert r.query_url == (
        "https://my-s4.example.com/sap/opu/odata/sap/API_SALES_ORDER_SRV/Customer"
        "?$filter=Country%20eq%20'DE'&$select=Customer,CustomerName"
    )
    assert r.confidence == 0.88 and r.version == "v2"  # the JSON tool details declare v2


def test_close_runner_up_is_passed_to_the_llm(session, jev, llm):
    scores = {"SalesOrder": 0.80, "SalesOrderItem": 0.75, "Customer": 0.1}
    jev.rule = rules(entity_sets=lambda it: scores[it["name"]])
    llm.candidates = [cand("SalesOrderItem", **{"$filter": "Material eq 'PUMP-01'"})]
    r = session.text_to_odata("Which orders contain material PUMP-01?", TOOLS, today=TODAY)
    assert [s.name for s in r.entity_sets if s.selected] == ["SalesOrder", "SalesOrderItem"]
    prompt = user_prompt(llm)
    assert "## SalesOrder (Sales Order Header)" in prompt and "## SalesOrderItem (Sales Order Item)" in prompt
    assert "More than one entity set might answer the question" in prompt
    assert r.entity_set == "SalesOrderItem" and not r.warnings


def test_single_entity_set_skips_selection(session, jev, llm):
    tools = {"entity_sets": [{"name": "Things", "keys": ["ID"], "properties": [{"name": "ID"}, {"name": "Name"}]}]}
    llm.candidates = [cand("Things", **{"$top": 3})]
    r = session.text_to_odata("Show three things", tools, today=TODAY)
    assert r.entity_sets[0].probability is None and r.version == "v4"
    assert jev_requests(jev, "entity_sets") == [] and len(jev_requests(jev, "candidates")) == 1
    assert r.query_url == "Things?$top=3"  # no service URL: relative


# ------------------------------------------------------------------------------------------------ pruning

RELEVANT = {"TotalNetAmount", "SoldToParty", "SalesOrderDate", "to_Customer", "Country", "CustomerName"}


def field_rule(item):
    name = item.get("property") or item.get("navigation_property")
    return 0.9 if name in RELEVANT else 0.05


def test_property_pruning_shrinks_wide_entities(session, jev, llm):
    jev.rule = rules(entity_sets=lambda it: 0.9 if it["name"] == "SalesOrder" else 0.1, fields=field_rule)
    llm.candidates = [
        cand("SalesOrder", **{"$filter": "to_Customer/Country eq 'DE'", "$orderby": "TotalNetAmount desc", "$top": 5})
    ]
    r = session.text_to_odata("Top 5 orders by net value from German customers", TOOLS, today=TODAY)

    fields = jev_requests(jev, "fields")
    stage_a = [it for req in fields for it in req["state"]["fields"]]
    assert len(stage_a) == 42 + 2  # 42 properties + 2 navigation properties, nothing else
    assert sorted(len(req["state"]["fields"]) for req in fields) == [4, 20, 20]
    assert fields[0]["questions"]["r0"]["instructions"] == FIELD_Q.format(ref="fields[0]")
    status = next(it for it in stage_a if it.get("property") == "OverallSDProcessStatus")
    assert status["values"][0] == "A = Not yet processed (open)"

    prompt = user_prompt(llm)
    for name in ("TotalNetAmount", "SoldToParty", "SalesOrderDate", "- SalesOrder (Edm.String, max 10, key)"):
        assert name in prompt
    assert "IncotermsLocation1" not in prompt and "AccountingExchangeRate" not in prompt
    assert "(34 more properties not shown" in prompt  # 8 kept: key + 3 relevant + 4 to reach min_properties
    assert "- to_Customer -> Customer (single)" in prompt and "  - Country (Edm.String, max 3)" in prompt
    assert "- to_Item -> SalesOrderItem (collection): Items of the order [target properties not shown]" in prompt
    assert r.entity_set == "SalesOrder"
    # the validator still checks against the full metadata, not the pruned prompt
    assert "to_Customer/Country" in r.candidates[0].query


def test_small_entities_are_not_pruned(session, jev, llm):
    jev.rule = rules(entity_sets=lambda it: 0.9 if it["name"] == "Customer" else 0.1)
    llm.candidates = [cand("Customer", **{"$top": 1})]
    session.text_to_odata("Any customer", TOOLS, today=TODAY)
    assert jev_requests(jev, "fields") == []
    assert "IsBlocked" not in user_prompt(llm) and "DeletionIndicator" in user_prompt(llm)


# ------------------------------------------------------------------------------------------------ validation + repair


def test_invented_property_is_rejected_and_repaired(session, jev, llm):
    jev.rule = rules(entity_sets=lambda it: 0.9 if it["name"] == "Customer" else 0.1)
    llm.candidates = [
        cand("Customer", **{"$filter": "CustomerCountry eq 'DE'"}),
        cand("Customer", **{"$filter": "Country eq 'DE'"}),
    ]
    llm.repairs.append(cand("Customer", "fixed the name", **{"$filter": "Country eq 'DE'", "$top": 50}))
    r = session.text_to_odata("Customers in Germany", TOOLS, n_candidates=2, today=TODAY)

    repairs = llm.repair_requests()
    assert len(repairs) == 1 and repairs[0]["temperature"] == 0.0
    feedback = repairs[0]["messages"][-1]["content"]
    assert "unknown property 'CustomerCountry' on Customer (did you mean 'Customer' or 'Country'?)" in feedback
    assert json.loads(repairs[0]["messages"][-2]["content"])["query_options"] == {"$filter": "CustomerCountry eq 'DE'"}
    first = r.candidates[0]
    assert first.repaired and first.valid and first.query_options == {"$filter": "Country eq 'DE'", "$top": 50}
    assert all(c.valid for c in r.candidates)


def test_without_repair_invalid_candidates_are_dropped(settings, jev, llm):
    settings.repair = False
    jev.rule = rules(entity_sets=lambda it: 0.9 if it["name"] == "Customer" else 0.1)
    llm.candidates = [
        cand("Customer", **{"$filter": "Land eq 'DE'"}),
        cand("Customer", **{"$filter": "Country eq 'DE'"}),
    ]
    with Session(settings) as s:
        r = s.text_to_odata("Customers in Germany", TOOLS, n_candidates=2, today=TODAY)
    assert llm.repair_requests() == []
    assert [c.valid for c in r.candidates] == [False, True]
    assert r.query_options == {"$filter": "Country eq 'DE'"}
    assert len(jev_requests(jev, "candidates")[0]["state"]["candidates"]) == 1  # only valid ones reach Jev


def test_no_valid_candidate_raises_with_reasons(session, jev, llm):
    jev.rule = rules(entity_sets=lambda it: 0.9 if it["name"] == "Customer" else 0.1)
    llm.candidates = [cand("Customer", **{"$filter": "Land eq 'DE'"}), "not json at all"]
    llm.repairs.extend([cand("Customer", **{"$filter": "Nation eq 'DE'"}), cand("Clients", **{})])
    with pytest.raises(NoValidQueryError, match="none of the 2 candidates") as e:
        session.text_to_odata("Customers in Germany", TOOLS, n_candidates=2, today=TODAY)
    msgs = [str(i) for c in e.value.candidates for i in c.issues if i.severity == "error"]
    assert any("Nation" in m for m in msgs) and any("unknown entity set 'Clients'" in m for m in msgs)
    assert (
        "the reply was not a JSON object" in llm.repair_requests()[1]["messages"][-1]["content"]
        or "the reply was not a JSON object" in llm.repair_requests()[0]["messages"][-1]["content"]
    )
    assert e.value.stats["llm"]["total"]["requests"] == 4
    assert jev_requests(jev, "candidates") == []


# ------------------------------------------------------------------------------------------------ v2 vs v4


def test_v2_and_v4_prompts_and_syntax(session, jev, llm):
    jev.rule = rules(entity_sets=lambda it: 0.9 if it["name"] in ("A_Customer", "Customers") else 0.1)
    # V4 service: contains() is right
    llm.candidates = [cand("Customers", **{"$filter": "contains(Name,'Acme')", "$count": True})]
    r4 = session.text_to_odata("How many customers have Acme in their name?", FIXTURES / "sales_v4.xml", today=TODAY)
    sys4 = system_prompt(llm)
    assert "OData V4 syntax" in sys4 and "contains(Name,'abc')" in sys4 and "$count=true" in sys4
    assert "substringof('abc',Name)" not in sys4
    assert r4.version == "v4" and r4.query_path == "Customers?$filter=contains(Name,'Acme')&$count=true"

    # V2 service: the same V4-style answer is rejected, repaired to substringof, and $count becomes $inlinecount
    llm.reset()
    jev.requests.clear()
    llm.candidates = [cand("A_Customer", **{"$filter": "contains(CustomerName,'Acme')", "$count": True})]
    llm.repairs.append(cand("A_Customer", **{"$filter": "substringof('Acme',CustomerName)", "$count": True}))
    r2 = session.text_to_odata(
        "How many customers have Acme in their name?",
        FIXTURES / "sales_v2.xml",
        service_url="https://s4/sap/opu/odata/sap/API_SALES_ORDER_SRV",
        today=TODAY,
    )
    sys2 = system_prompt(llm)
    assert "OData V2 syntax, SAP Gateway compatible" in sys2 and "$inlinecount=allpages" in sys2
    assert (
        "V2 has no contains(); use substringof('text',Property)" in llm.repair_requests()[0]["messages"][-1]["content"]
    )
    assert r2.version == "v2"
    assert r2.query_url == (
        "https://s4/sap/opu/odata/sap/API_SALES_ORDER_SRV/A_Customer"
        "?$filter=substringof('Acme',CustomerName)&$inlinecount=allpages"
    )
    verify = jev_requests(jev, "candidates")[0]["state"]
    assert verify["odata_version"] == "2.0" and verify["today"] == TODAY


def test_explicit_version_overrides_metadata_with_a_warning(session, jev, llm):
    jev.rule = rules(entity_sets=lambda it: 0.9 if it["name"] == "Customer" else 0.1)
    llm.candidates = [cand("Customer", **{"$filter": "contains(CustomerName,'Acme')"})]
    r = session.text_to_odata("Acme customers", TOOLS, "v4", today=TODAY)
    assert r.version == "v4" and "metadata is OData V2 but the query is OData V4" in r.warnings[0]


# ------------------------------------------------------------------------------------------------ reranking


def test_reranking_picks_the_candidate_jev_prefers(session, jev, llm):
    jev.rule = rules(
        entity_sets=lambda it: 0.9 if it["name"] == "SalesOrder" else 0.1,
        fields=field_rule,
        candidates=lambda it: 0.92 if "TotalNetAmount desc" in it["query"] else 0.35,
    )
    llm.candidates = [
        cand("SalesOrder", "smallest first", **{"$orderby": "TotalNetAmount asc", "$top": 5}),
        cand("SalesOrder", "largest first", **{"$orderby": "TotalNetAmount desc", "$top": 5}),
        cand("SalesOrder", "largest first again", **{"$top": "5", "orderby": "TotalNetAmount desc"}),
    ]
    r = session.text_to_odata("What are the five largest orders by net value?", TOOLS, today=TODAY)
    verify = jev_requests(jev, "candidates")
    assert len(verify) == 1 and len(verify[0]["state"]["candidates"]) == 2  # duplicates merged before Jev
    assert verify[0]["questions"]["r0"]["instructions"] == VERIFY_Q.format(ref="candidates[0]")
    item = verify[0]["state"]["candidates"][1]
    assert item["fields"]["TotalNetAmount"] == "Net Value"
    assert r.query_options == {"$orderby": "TotalNetAmount desc", "$top": 5}
    assert r.confidence == 0.92 and r.explanation == "largest first"
    assert [(c.probability, c.votes) for c in r.candidates] == [(0.35, 1), (0.92, 2), (0.92, 2)]


def test_confidence_comes_from_jev_not_from_the_majority(session, jev, llm):
    jev.rule = rules(
        entity_sets=lambda it: 0.9 if it["name"] == "Customer" else 0.1,
        candidates=lambda it: 0.81 if "CityName" in it["query"] else 0.4,
    )
    llm.candidates = [
        cand("Customer", **{"$filter": "Country eq 'DE'"}),
        cand("Customer", **{"$filter": "Country eq 'DE'"}),
        cand("Customer", **{"$filter": "CityName eq 'Berlin'"}),
    ]
    r = session.text_to_odata("Customers in Berlin", TOOLS, today=TODAY)
    assert r.query_options == {"$filter": "CityName eq 'Berlin'"} and r.confidence == 0.81


# ------------------------------------------------------------------------------------------------ residual conditions


def test_residual_condition_and_judge_rows(session, jev, llm):
    jev.rule = rules(
        entity_sets=lambda it: 0.9 if it["name"] == "SalesOrder" else 0.1,
        fields=field_rule,
        candidates=lambda it: 0.77 if it.get("residual_condition") else 0.3,
    )
    llm.candidates = [
        cand(
            "SalesOrder",
            "Open orders; tone needs judgment.",
            "the customer comment sounds angry",
            **{"$filter": "OverallSDProcessStatus eq 'A'", "$select": "SalesOrder,CustomerComment"},
        ),
        cand(
            "SalesOrder",
            "Keyword match.",
            **{"$filter": "OverallSDProcessStatus eq 'A' and substringof('angry',CustomerComment)"},
        ),
    ]
    r = session.text_to_odata("Open orders where the customer sounds angry", TOOLS, n_candidates=2, today=TODAY)
    assert r.residual_condition == "the customer comment sounds angry"
    assert r.query_options["$select"] == "SalesOrder,CustomerComment" and r.confidence == 0.77
    verify = jev_requests(jev, "candidates")[0]["state"]["candidates"]
    assert verify[0]["residual_condition"] == "the customer comment sounds angry"
    assert verify[0]["fields"]["OverallSDProcessStatus"].startswith("Overall Status (A = Not yet processed")

    payload = {
        "d": {
            "results": [
                {
                    "__metadata": {"uri": "x"},
                    "SalesOrder": "1",
                    "CustomerComment": "Third late delivery, I am ANGRY",
                    "to_Item": {"__deferred": {"uri": "y"}},
                },
                {"__metadata": {"uri": "x"}, "SalesOrder": "2", "CustomerComment": "Thanks, all fine"},
            ]
        }
    }
    rows = extract_records(payload)
    jev.rule = None  # pg-jev's mock rule: 0.9 if the last word of the condition appears in the row
    judged = session.judge_rows(rows, r.residual_condition)
    assert [j.probability for j in judged] == [0.9, 0.1] and [j.passed for j in judged] == [True, False]
    sent = jev_requests(jev, "rows")[0]
    assert sent["state"] == {
        "condition": "the customer comment sounds angry",
        "rows": [
            {"SalesOrder": "1", "CustomerComment": "Third late delivery, I am ANGRY"},
            {"SalesOrder": "2", "CustomerComment": "Thanks, all fine"},
        ],
    }
    assert sent["questions"]["r1"]["instructions"] == ROW_Q.format(ref="rows[1]")
    assert session.filter_rows(rows, r.residual_condition) == [rows[0]]
    assert len(jev_requests(jev, "rows")) == 1  # second call answered from the cache


def test_judge_rows_batches_and_limits_fields(session, jev):
    rows = [{"id": i, "text": "angry" if i % 3 == 0 else "calm", "secret": "x"} for i in range(45)]
    judged = session.judge_rows(rows, "the text is angry", fields=["id", "text"])
    assert sum(j.passed for j in judged) == 15
    reqs = jev_requests(jev, "rows")
    assert sorted(len(r["state"]["rows"]) for r in reqs) == [5, 20, 20]
    assert all(set(row) == {"id", "text"} for r in reqs for row in r["state"]["rows"])
    session.judge_rows(rows, "the text is angry", fields=["text"])  # identical rows are judged once
    assert len(jev_requests(jev, "rows")[-1]["state"]["rows"]) == 2


def test_record_helpers():
    assert extract_records({"value": [{"a": 1}]}) == [{"a": 1}]
    assert extract_records({"d": [{"a": 1}]}) == [{"a": 1}]
    assert extract_records({"d": {"a": 1}}) == [{"a": 1}]
    assert extract_records([{"a": 1}]) == [{"a": 1}]
    assert clean_record({"@odata.etag": "W/1", "a": {"results": [1]}, "b": [{"__metadata": {}, "c": 2}]}) == {
        "a": {"results": [1]},
        "b": [{"c": 2}],
    }


# ------------------------------------------------------------------------------------------------ stats + module API


def test_stats_per_stage_and_session(session, jev, llm):
    jev.rule = rules(entity_sets=lambda it: 0.9 if it["name"] == "SalesOrder" else 0.1, fields=field_rule)
    llm.candidates = [
        cand("SalesOrder", **{"$filter": "SoldToParty eq '1000'"}),
        cand("SalesOrder", **{"$filter": "X eq 1"}),
    ]
    llm.repairs.append(cand("SalesOrder", **{"$filter": "SoldToParty eq '1000'"}))
    r = session.text_to_odata("Orders of customer 1000", TOOLS, n_candidates=2, today=TODAY)
    s = r.stats
    assert s["jev"]["select"]["requests"] == 1 and s["jev"]["prune"]["requests"] == 3
    assert s["jev"]["verify"]["requests"] == 1 and s["jev"]["verify"]["items"] == 1  # merged duplicates
    assert s["llm"]["generate"]["requests"] == 2 and s["llm"]["repair"]["requests"] == 1
    assert s["jev"]["total"]["estimated_cost_usd"] > 0 and s["llm"]["total"]["estimated_cost_usd"] is None
    assert s["llm_cost_known"] is False and s["estimated_cost_usd"] == s["jev"]["total"]["estimated_cost_usd"]
    session.text_to_odata("Orders of customer 1000", TOOLS, n_candidates=2, today=TODAY)
    total = session.stats()
    assert total["jev"]["total"]["cache_hits"] > 0  # selection, pruning and verification answers were cached
    assert total["jev"]["total"]["requests"] == 5 and total["llm"]["total"]["requests"] == 5
    assert total["jev"]["cached_answers"] == 3 + 44 + 1


def test_module_function_with_settings(settings, jev, llm):
    jev.rule = rules(entity_sets=lambda it: 0.9 if it["name"] == "Customer" else 0.1)
    llm.candidates = [cand("Customer", **{"$top": 2})]
    r = text_to_odata("Two customers", json.loads(TOOLS.read_text()), settings=settings, today=TODAY)
    assert r.entity_set == "Customer" and r.query_path == "Customer?$top=2"
