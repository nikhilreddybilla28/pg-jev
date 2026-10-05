# odata-jev

Turn a plain-language question into an OData query. An LLM writes candidate queries; TypeSafe's
[Jev](https://docs.typesafe.ai) picks the entity set, trims the metadata the LLM sees and verifies the result,
so the confidence score is a calibrated probability and not the LLM's opinion of itself.

```python
from odata_jev import text_to_odata

r = text_to_odata(
    "Top 5 open orders from German customers by net value",
    "API_SALES_ORDER_SRV.xml",                      # the service's $metadata, or JSON tool details
    service_url="https://my-s4.example.com/sap/opu/odata/sap/API_SALES_ORDER_SRV",
)
r.query_url     # for example:
# https://my-s4.example.com/sap/opu/odata/sap/API_SALES_ORDER_SRV/A_SalesOrder?$filter=OverallSDProcessStatus%20eq%20'A'%20and%20to_SoldToParty/Country%20eq%20'DE'
#   &$orderby=TotalNetAmount%20desc&$top=5
r.confidence    # Jev's probability that this query answers the question, e.g. 0.91
r.explanation   # the LLM's one-line rationale for the chosen candidate
```

```bash
odata-jev "Top 5 open orders from German customers by net value" --metadata API_SALES_ORDER_SRV.xml
```

OData V4 is the default; V2 (SAP Gateway) is used when the `$metadata` says so or when you pass `version="v2"`.

The design borrows from [pg-jev](https://github.com/realZachi/pg-jev), which uses Jev as a row-level yes/no judge
inside PostgreSQL. Jev never writes a query here either. See [DESIGN.md](DESIGN.md) for the data flow.

## How it works

1. **Load the tools.** JSON tool details or a `$metadata` document (V2 with SAP `sap:` annotations, V4 with
   Core/Common/Capabilities annotations) become one model: entity sets, properties, keys, navigation properties.
2. **Pick the entity set.** Jev answers "Can the entity set `entity_sets[i]` answer the question?" for every set,
   20 per request. The top set goes to the LLM, plus the runner-up when the two are within 0.1.
3. **Prune properties.** For entity types with more than 30 properties and navigations (hundreds is normal in SAP),
   Jev answers "Is `fields[i]` needed to answer the question?". Keys always stay; at least 8 and at most 60
   properties go into the prompt. Targets of relevant navigation properties are pruned the same way.
4. **Generate.** 3 chat calls at temperature 0.7 return `{entity_set, query_options, residual_condition, rationale}`
   in JSON mode. The system prompt carries a grammar cheat sheet for the target version.
5. **Validate.** Every entity set, property, navigation path, function, operator and literal is checked against the
   full metadata and the version. Invalid candidates get one repair call that lists the problems.
6. **Verify.** Jev answers "Does the OData query `candidates[i]` correctly answer the question?" for each distinct
   valid candidate. The highest probability wins and becomes `confidence`.

## Install

```bash
pip install -e ".[dev]"     # Python 3.11+; runtime deps: httpx, pydantic, lxml
```

## Settings

Environment variables, a `.env` file in the current directory, or a `Settings(...)` object passed as `settings=`.
Copy `.env.example` to `.env` and fill in the keys; `.env` is git-ignored, and variables already set in the
environment win over it (`ODATA_JEV_ENV_FILE` points elsewhere, empty turns the file off).

| Variable | Default | Meaning |
| --- | --- | --- |
| `TYPESAFE_API_KEY` | | Jev API key from https://console.typesafe.ai. Only required for `*.typesafe.ai` hosts |
| `JEV_API_URL` | `https://api.typesafe.ai/v1/systemone` | Any server speaking the same contract (stuntd, mocks) |
| `JEV_MODEL` | `jev-latest` | Pin a version such as `jev-1.13.0` when results feed reports |
| `JEV_BATCH_SIZE` | `20` | Items per Jev request. pg-jev measured accuracy dropping above ~20 |
| `JEV_CONCURRENCY` | `16` | Parallel Jev requests over pooled keep-alive connections |
| `JEV_TIMEOUT` | `30` | Seconds per Jev request |
| `JEV_KEEPALIVE` | `600` | Seconds an idle pooled connection is kept |
| `JEV_MAX_ITEMS_PER_CALL` / `JEV_MAX_CHARS_PER_CALL` | `0` (off) | Spend guard: refuse a call that would send more |
| `LLM_API_KEY` | | Bearer token for the chat API (not needed for local servers) |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | Any OpenAI-compatible base URL (vLLM, Ollama, LiteLLM, Azure proxy, …) |
| `LLM_MODEL` | (required) | Chat model name |
| `LLM_TEMPERATURE` | `0.7` | Sampling temperature for the N candidates; repairs run at 0 |
| `LLM_JSON_MODE` | `json_object` | `json_object`, `json_schema` (structured outputs) or `none` |
| `LLM_SEED` | `0` | Candidate *i* is sent with seed `LLM_SEED + i`; `none` sends no seed |
| `LLM_CONCURRENCY` / `LLM_TIMEOUT` | `4` / `60` | |
| `LLM_PRICE_INPUT_PER_MTOK` / `LLM_PRICE_OUTPUT_PER_MTOK` | unset | USD per million tokens; without them LLM cost is reported as unknown |
| `ODATA_VERSION` | `v4` | Used when neither the call nor the metadata names a version |
| `ODATA_JEV_CANDIDATES` | `3` | Candidates per question |
| `ODATA_JEV_REPAIR` | `on` | One repair call per invalid candidate |

Pipeline thresholds (`selection_margin`, `prune_above`, `property_threshold`, `min_properties`, `max_properties`)
are `Settings` fields too, with `ODATA_JEV_` variables; see `odata_jev/settings.py`.

## Tool details as JSON

```json
{
  "service_url": "https://my-s4.example.com/sap/opu/odata/sap/API_SALES_ORDER_SRV",
  "version": "v2",
  "entity_sets": [
    {
      "name": "SalesOrder",
      "label": "Sales Order Header",
      "description": "One row per order with customer, dates, amounts and status",
      "keys": ["SalesOrder"],
      "searchable": true,
      "properties": [
        {"name": "SalesOrder", "type": "Edm.String", "max_length": 10, "label": "Sales Order"},
        {"name": "OverallSDProcessStatus", "type": "Edm.String", "label": "Overall Status",
         "values": {"A": "Not yet processed (open)", "B": "Partially processed", "C": "Completely processed"}},
        {"name": "HeaderBillingBlockReason", "type": "Edm.String", "filterable": false, "sortable": false}
      ],
      "navigation_properties": [{"name": "to_Item", "target": "SalesOrderItem", "collection": true}]
    }
  ]
}
```

`values` makes a property a closed code list: the validator rejects `OverallSDProcessStatus eq 'Open'` and tells the
LLM to use `'A'`. A full example is `tests/fixtures/sales_tools.json`.

## Conditions OData can't express

"Open orders where the customer sounds angry" has a part no `$filter` can state. The LLM splits it: the query
filters open orders and selects the comment, and `residual_condition` holds "the customer comment sounds angry".
Run the query yourself, then let Jev judge each record, batched 20 per request exactly like pg-jev's `jev()`:

```python
from odata_jev import extract_records, filter_rows

rows = extract_records(response.json())            # V4 {"value": [...]} or V2 {"d": {"results": [...]}}
angry = filter_rows(rows, r.residual_condition, fields=["SalesOrder", "CustomerComment"])
```

`judge_rows` returns each row with its probability. `fields` limits what leaves your system; OData bookkeeping
(`__metadata`, `@odata.*`, deferred navigation links) is always stripped.

## Result

| Field | |
| --- | --- |
| `query_url`, `query_path`, `query_string` | percent-encoded; `query_path` is relative to the service root |
| `query_options`, `parts` | the validated options, and each one rendered as it appears in the URL (decoded) |
| `confidence` | Jev's probability for the chosen candidate |
| `explanation` | the chosen candidate's rationale |
| `residual_condition` | `None`, or a condition for `judge_rows` |
| `entity_sets` | every set with its selection probability |
| `candidates` | every candidate: options, issues, whether it was repaired, its Jev probability and votes |
| `warnings`, `stats` | stats hold requests, tokens, cache hits, retries and estimated cost per stage |

Errors derive from `OdataJevError`. `NoValidQueryError.candidates` says why each candidate failed.

## What the validator checks

- the entity set exists and is addressable;
- every path in `$filter`, `$select`, `$orderby` and `$expand` resolves, through navigation and complex
  properties, with "did you mean" suggestions (names are case-sensitive);
- V2 vs V4: `substringof` vs `contains`, `datetime'2024-01-31T00:00:00'` vs `2024-01-31`, `guid'…'` vs bare GUIDs,
  `$inlinecount=allpages` vs `$count=true`, `search` vs `$search`, `Nav/Prop` with `$expand=Nav` vs nested
  `$expand=Nav($select=…)`, no `any`/`all` or `in` in V2;
- literal types against property types (`SalesOrder eq 12345` on an `Edm.String` key is an error), `substringof`
  with swapped arguments, invalid dates, code lists;
- the grammar itself, against the OData 4.01 ABNF and the Part 2 precedence table: `has`/`in` bind tighter than
  `not`, relational before equality, enum literals (type name, flag lists, numeric members), `all()` needs a
  variable and every lambda body must use its variable, valid calendar dates and times, `$expand` item options
  (`*($levels=…)`, `/$ref(…)`, `/$count(…)`); see `tests/test_spec_conformance.py`;
- SAP and Capabilities flags: `sap:filterable`, `sap:sortable`, `sap:searchable`, `sap:pageable`,
  `sap:requires-filter`, `sap:required-in-filter`, `NonFilterableProperties`, `SearchRestrictions`.

Small slips are fixed without an LLM call and reported with severity `fixed`: `filter` → `$filter`, `"10"` → `10`,
list values joined, `$count` ↔ `$inlinecount` and `$search` ↔ `search` mapped to the version.

## Caveats

- The Jev instructions and the cheat sheets have only run against the mocks. pg-jev tuned its wording on real data;
  measure selection, pruning recall and verification separation on your own service before trusting the
  numbers (see DESIGN.md, "Not measured yet").
- The question and the trimmed metadata go to the LLM and to Jev; `judge_rows` sends record contents to Jev. Point
  `JEV_API_URL` and `LLM_BASE_URL` at local servers when that is not acceptable.
- odata-jev builds the query. It does not call the OData service.
- Aggregation (`$apply`) and V4 `$compute` are not supported.

## Development

```bash
pip install -e ".[dev]"
pytest              # starts a mock Jev server and a mock LLM on 127.0.0.1; never calls live APIs
ruff check . && ruff format --check .
```

`tests/mock_jev.py` adapts pg-jev's `test/mock_api.py`; `tests/mock_llm.py` answers chat completions with canned
JSON chosen by seed. Both also run standalone (`python tests/mock_jev.py 8765`).
