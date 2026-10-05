import json

import pytest

from odata_jev.cli import main

from .conftest import FIXTURES
from .test_pipeline import cand, rules


@pytest.fixture
def env(monkeypatch, jev, llm):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("JEV_API_URL", jev.url)
    monkeypatch.setenv("LLM_BASE_URL", llm.url)
    monkeypatch.setenv("LLM_MODEL", "mock-model")
    monkeypatch.setenv("JEV_RETRY_BASE_DELAY", "0")
    jev.rule = rules(
        entity_sets=lambda it: 0.9 if it["name"] in ("Customer", "Customers") else 0.2, candidates=lambda it: 0.87
    )
    llm.candidates = [
        cand("Customer", "Customers whose country is Germany.", **{"$filter": "Country eq 'DE'", "$top": 10})
    ]


def test_text_output(env, capsys):
    code = main(["German customers", "--metadata", str(FIXTURES / "sales_tools.json"), "--today", "2026-10-05"])
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith(
        "GET https://my-s4.example.com/sap/opu/odata/sap/API_SALES_ORDER_SRV/Customer"
        "?$filter=Country%20eq%20'DE'&$top=10"
    )
    assert "confidence   0.87 from Jev (3 of 3 candidates agree)" in out
    assert "explanation  Customers whose country is Germany." in out
    assert "$filter = Country eq 'DE'" in out
    assert "cost         Jev 2 requests" in out and "cost unknown" in out


def test_json_output_and_version_flag(env, llm, capsys):
    llm.candidates = [cand("Customers", **{"$filter": "Country eq 'DE'", "$count": True})]
    code = main(
        [
            "German customers",
            "-m",
            str(FIXTURES / "sales_v4.xml"),
            "--json",
            "-n",
            "1",
            "--service-url",
            "https://svc/odata/v4/sales",
        ]
    )
    data = json.loads(capsys.readouterr().out)
    assert code == 0 and data["version"] == "v4" and data["entity_set"] == "Customers"
    assert data["query_url"] == "https://svc/odata/v4/sales/Customers?$filter=Country%20eq%20'DE'&$count=true"
    assert data["stats"]["llm"]["total"]["requests"] == 1


def test_no_valid_query_exit_code(env, llm, capsys):
    llm.candidates = [cand("Customer", **{"$filter": "Land eq 'DE'"})]
    llm.repairs.append(cand("Customer", **{"$filter": "Nation eq 'DE'"}))
    code = main(["German customers", "-m", str(FIXTURES / "sales_tools.json"), "-n", "1"])
    err = capsys.readouterr().err
    assert code == 1 and "unknown property 'Nation'" in err


def test_config_and_metadata_errors(env, monkeypatch, capsys):
    assert main(["q", "-m", str(FIXTURES / "missing.json")]) == 2
    assert "cannot read tool details" in capsys.readouterr().err
    monkeypatch.delenv("LLM_MODEL")
    assert main(["q", "-m", str(FIXTURES / "sales_tools.json")]) == 2
    assert "Set LLM_MODEL" in capsys.readouterr().err
