---
name: odata-jev
description: Turn plain-language questions into OData V4 or V2 (SAP Gateway) queries with the odata-jev Python package, where an LLM writes candidate queries and TypeSafe's Jev selects the entity set, prunes properties and verifies the result with a calibrated confidence. Use this skill whenever a user wants to query an OData service or an SAP API (API_SALES_ORDER_SRV, S/4HANA, Gateway, CAP, RAP) in natural language, mentions odata-jev, text-to-OData, `text_to_odata`, `judge_rows`, `filter_rows`, a `$metadata` document or EDMX, `$filter`/`$expand` syntax differences between V2 and V4, or hits an error starting with `odata-jev:`.
---

# odata-jev: text-to-OData with a calibrated confidence

`text_to_odata(question, tools)` returns one validated OData query with `confidence` (Jev's probability that the
query answers the question), a short `explanation` and, when part of the question needs judgment that OData
can't express, a `residual_condition` to apply to the returned records with `filter_rows`.

The LLM only writes candidates. Jev never writes a query: it answers yes/no questions (which entity set, which
properties matter, which candidate is right). A deterministic validator sits between them, so a property name the
LLM invented never reaches the caller.

Source and design: `README.md` and `DESIGN.md` next to this skill's package (`odata-jev/`).

## Figure out which job you have

| The user wants… | Do this |
| --- | --- |
| to set it up | **Install and configure** below |
| a query for a question | **Generate a query** below |
| to filter results by tone, intent or other meaning | **Residual conditions** below |
| to know why a query was rejected or looks wrong | **Read the result** and **Troubleshoot** below |
| to change thresholds, models, spend limits | the settings table in `README.md` (`odata_jev/settings.py` is the source of truth) |

## Install and configure

```bash
pip install -e ./odata-jev            # Python 3.11+; installs httpx, pydantic, lxml
export TYPESAFE_API_KEY=...           # https://console.typesafe.ai; not needed for a local JEV_API_URL
export LLM_BASE_URL=https://api.openai.com/v1 LLM_API_KEY=... LLM_MODEL=...   # any OpenAI-compatible server
```

Or copy `odata-jev/.env.example` to `.env` in the directory you run from and fill it in; `Settings.from_env()`
reads it, and real environment variables win over it. `.env` is git-ignored. Never write a user's real keys into
files you commit. Optional: `LLM_PRICE_INPUT_PER_MTOK` and
`LLM_PRICE_OUTPUT_PER_MTOK` so LLM cost shows up in the stats (Jev cost is always estimated at $0.042 per million
input tokens). `JEV_MAX_ITEMS_PER_CALL` caps what one call may send to Jev.

## Generate a query

Get the service description first, in one of two forms:

- the service's `$metadata` document (`GET <service root>/$metadata`, save it as `.xml`). V2 vs V4 is detected from
  it, and SAP annotations (`sap:filterable`, `sap:searchable`, `sap:label`, …) are used. Pass `service_url=` too,
  because EDMX does not contain the service root;
- or JSON tool details (`tests/fixtures/sales_tools.json` is a complete example). Add `values` to code fields
  (status, type, category) with their meanings: the LLM then uses `'A'` instead of `'Open'`, and the validator
  enforces it. Add `label` and `description` wherever names are cryptic, which in SAP is almost everywhere.

```python
from odata_jev import text_to_odata
r = text_to_odata("open orders from German customers, biggest first", "API_SALES_ORDER_SRV.xml",
                  service_url="https://host/sap/opu/odata/sap/API_SALES_ORDER_SRV")
print(r.query_url, r.confidence, r.explanation)
```

```bash
odata-jev "open orders from German customers, biggest first" --metadata API_SALES_ORDER_SRV.xml \
  --service-url https://host/sap/opu/odata/sap/API_SALES_ORDER_SRV      # add --json for everything, -v for logs
```

For repeated questions against the same service, reuse one `Session(settings)`: it keeps connections warm and
caches Jev's answers, so selection and pruning for a known question cost nothing the second time.

Phrase questions with the exact condition. "Orders over 10,000 EUR created this year" beats "big recent orders":
the LLM turns relative dates into literals using today's date (pass `today=` for reproducible runs).

## Residual conditions

When the question mixes exact filters with meaning ("open orders where the customer sounds angry"), the LLM puts
the exact part into `$filter` and returns the rest as `residual_condition`. odata-jev does not call the OData
service; run the query, then:

```python
from odata_jev import extract_records, filter_rows
rows = extract_records(response.json())                       # V2 {"d": {"results": …}} or V4 {"value": …}
kept = filter_rows(rows, r.residual_condition, fields=["SalesOrder", "CustomerComment"])
```

Each record goes to Jev, 20 per request (pg-jev's row format). Pass `fields` so only what the judgment needs
leaves the user's system, and say so when records may contain personal data. Use `judge_rows` to see
probabilities and pick a threshold instead of the default 0.5.

## Read the result

- `confidence` comes from Jev. Below ~0.5 the query probably misses part of the question; read `explanation` and
  `candidates`.
- `entity_sets` shows Jev's probability per entity set. A warning "no entity set clearly answers the question"
  means the metadata may not hold what the user asks for; say that rather than presenting the query as an answer.
- `candidates[i].issues` lists validation problems (`error`), automatic fixes (`fixed`) and notes (`warning`).
  `repaired=True` means the first attempt failed validation and one repair call fixed it.
- `stats` has requests, tokens and estimated cost per stage (`select`, `prune`, `verify`, `generate`, `repair`).

## Troubleshoot

| Message | Cause / fix |
| --- | --- |
| `odata-jev: no TypeSafe API key` | Set `TYPESAFE_API_KEY`, or point `JEV_API_URL` at a local Jev-compatible server. |
| `odata-jev: no LLM model` | Set `LLM_MODEL` (and `LLM_BASE_URL`, `LLM_API_KEY` for hosted APIs). |
| `none of the N candidates passed validation` | Read each candidate's errors (the CLI prints them). Usually the metadata lacks the property the question needs, or a code field has no `values`. Fix the tool details or rephrase. |
| `unknown property 'X' … (did you mean 'Y'?)` | The LLM guessed a name. If it happens often, add `label`/`description` to the real property. |
| `V2 has no contains(); use substringof(...)` and similar | The LLM wrote the other version's syntax. Check the version: the metadata decides unless `version=` overrides it. |
| `X is not filterable in this service` | The service forbids it (`sap:filterable="false"` or Capabilities); filter on another property or use `residual_condition`. |
| `JEV_MAX_ITEMS_PER_CALL` / `JEV_MAX_CHARS_PER_CALL` | The spend guard fired: `filter_rows` on too many rows. Filter more in OData first, or raise the guard on purpose. |
| `TypeSafe API error 401` / `LLM API error 401` | Wrong key for that endpoint. |

Things to be straight about with users: the Jev prompts and cheat sheets have not been measured on real services
yet (see `DESIGN.md`), the question and trimmed metadata go to both APIs, and odata-jev does not run the query.
