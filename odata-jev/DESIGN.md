# odata-jev design

`odata-jev` turns a plain-language question plus OData service metadata into one OData query (V4 by default, V2
on request) with a confidence score and a short explanation.

Two models split the work:

- An **LLM** behind any OpenAI-compatible chat API writes candidate queries as JSON.
- **Jev** (TypeSafe's System One API, the same `POST /v1/systemone` contract pg-jev uses) answers yes/no questions
  with calibrated probabilities. It never writes a query. It picks entity sets, prunes properties, ranks candidates
  and judges returned records.

A deterministic validator sits between them. No candidate reaches Jev or the caller unless every entity set,
property, navigation path, function and literal in it checks out against the metadata.

## What we take from pg-jev, and what we don't

pg-jev asks Jev one question per table row: "does `rows[i]` satisfy `condition`?". It packs 20 rows into one shared
`state`, keeps requests in flight on a thread pool over keep-alive connections, caches each answer by content hash
for the session and retries on 408/429/529/5xx with `Retry-After`. Its regression tests run against
`test/mock_api.py`, a deterministic fake of the API.

odata-jev keeps that client design (`jev_client.py`) and the batch format, and asks Jev different questions:

| Stage | Items in one shared state | Question per item |
| --- | --- | --- |
| entity set selection | one summary per entity set | Can the entity set `entity_sets[i]` answer the question stated in `question`? |
| property pruning | properties and navigation properties of the chosen set(s) | Is `fields[i]` needed to answer the question stated in `question`? |
| candidate verification | validated candidate queries | Does the OData query `candidates[i]` correctly answer the question stated in `question`? |
| row post-filter (`judge_rows`) | returned records, pg-jev's exact format | Does the record `rows[i]` satisfy the condition stated in `condition`? |

pg-jev streams a relation and answers rows as the executor asks for them. We know all items up front, so there is no
read-ahead or skip list. A stage submits all of its batches at once and waits for them.

## Data flow

```
question + tools (JSON tool details | $metadata EDMX v2/v4)
  │
  ▼ metadata.py      normalise to Service → EntitySet → EntityType → Property / NavigationProperty
  │
  ▼ select           Jev noul over entity set summaries (20 per request)
  │                  keep the top set; also keep the runner-up when p1 - p2 < selection_margin (0.1)
  │                  one entity set: skip Jev
  │
  ▼ prune            sets with more than prune_above (30) properties + navigations:
  │                    stage A: Jev noul over the set's own properties and navigation properties
  │                    stage B: Jev noul over target properties of the navigations kept in stage A
  │                  keep p ≥ property_threshold (0.3), always keep keys, at least min_properties (8) by p,
  │                  at most max_properties (60)
  │
  ▼ generator.py     prompt = rules + grammar cheat sheet for the version + pruned metadata + today's date
  │                  N (3) chat calls at temperature 0.7 with seeds 0..N-1, run concurrently, JSON mode:
  │                  {entity_set, query_options, residual_condition, rationale}
  │
  ▼ validator.py     normalise option keys and value types, parse $filter/$orderby/$select/$expand,
  │                  resolve every path against the full (unpruned) metadata, check functions and literal
  │                  formats for the version, type-check comparisons, SAP capability flags
  │                  invalid → one repair call per invalid candidate (temperature 0) with the issues listed
  │                  → validate again; still invalid → dropped
  │
  ▼ builder.py       render + percent-encode each candidate; identical queries merge and count as votes
  │
  ▼ verify           Jev noul over the distinct valid candidates
  │                  winner = highest p (ties: more votes, then earlier candidate)
  │                  confidence = Jev's p for the winner; explanation = the winner's rationale
  │
  ▼ Result           entity_set, query_url, query_options, parts, confidence, explanation,
                     residual_condition, candidates, entity set scores, warnings, stats
```

When the question carries a condition OData can't express ("customers who sound angry"), the LLM returns it as
`residual_condition` and selects the fields needed to judge it. The caller runs the query, then
`judge_rows(records, residual_condition)` (or `filter_rows`) applies Jev to each record, batched exactly like
pg-jev's `jev()`.

## Modules

| Module | Responsibility |
| --- | --- |
| `settings.py` | `Settings` (pydantic), read from env vars by `Settings.from_env()` |
| `errors.py` | `OdataJevError` and subclasses |
| `stats.py` | thread-safe counters per stage label: requests, tokens, items, cache hits, retries, errors, ms, cost |
| `metadata.py` | JSON tool details and EDMX (V2 incl. SAP annotations, V4 incl. Core/Common/Capabilities) → `Service` |
| `versions.py` | per-version tables: functions, literal forms, query options, the prompt cheat sheets |
| `expression.py` | tokenizer, recursive-descent parser and AST for OData common expressions |
| `validator.py` | `validate(service, entity_set, options, version)` → normalised options, issues, referenced fields |
| `builder.py` | `build_query(...)` → URL, relative path, query string, each option separately |
| `jev_client.py` | batched noul questions, thread pool, httpx keep-alive pool, retries, cache, spend guard, stats |
| `llm_client.py` | `/chat/completions` with JSON mode, retries, concurrency, token stats, JSON extraction |
| `generator.py` | system and user prompts, candidate parsing, repair prompt |
| `pipeline.py` | `Session`, `text_to_odata`, `judge_rows`, `filter_rows` |
| `cli.py` | `odata-jev "question" --metadata file.xml\|file.json` |

## Decisions

- **Raw OData strings in `query_options`.** LLMs write `$filter` text well and badly-formed ASTs often. The validator
  parses the text, so the returned options are what the LLM wrote after normalisation, never re-serialised.
- **Validation runs against the full metadata, prompts use the pruned metadata.** A property the pruning step hid but
  the LLM still named correctly is real, so it passes. A name that exists nowhere fails, with "did you mean"
  suggestions fed to the repair call.
- **Deterministic normalisation before any LLM repair**: `filter` → `$filter`, `"10"` → `10`, list values joined,
  `$count` ↔ `$inlinecount` and `$search` ↔ SAP's `search` mapped to the target version. Each fix is reported as an
  issue with severity `fixed`.
- **Strict where servers are strict.** V2 `datetime'…'` needs a time part (SAP Gateway rejects a bare date); quoted
  numbers against numeric properties and unquoted numbers against string properties (SAP NUMC keys are strings) are
  errors; V2 has no `contains`, `any`/`all` or nested `$expand` options; V4 has no `substringof`, `datetime'…'` or
  `$inlinecount`. `substringof(Prop, 'x')` with the arguments swapped is caught.
- **Separate chat calls per candidate** instead of the `n` parameter, because many OpenAI-compatible servers ignore
  or reject `n`. Each call sends `seed = LLM_SEED + i`, which also lets the mock LLM give candidate `i` a fixed
  answer.
- **Jev cache key** = sha1 of `[model, instruction, items_key, shared state, item]`. pg-jev keys on relation, question
  and row content; ours also covers the shared state, since a property question is only meaningful with its
  question.
- **Spend guard per call** (`JEV_MAX_ITEMS_PER_CALL`, `JEV_MAX_CHARS_PER_CALL`), the counterpart of pg-jev's
  per-statement guard, matters most for `judge_rows` on large result sets.
- **Cost** uses TypeSafe's list price for Jev ($0.042 per million input tokens, output free) like pg-jev. LLM cost
  is only estimated when `LLM_PRICE_INPUT_PER_MTOK` / `LLM_PRICE_OUTPUT_PER_MTOK` are set; otherwise it is `null`.

## Not measured yet

pg-jev's prompt wording and batch size were tuned on real data (20 rows per request: 100 % accurate; 40: 92–98 %).
The four instructions above and the cheat sheets have only run against the mocks. Before relying on the confidence
numbers, measure on a real service: selection accuracy against hand-labelled questions, recall of pruning (a needed
property dropped is fatal, an extra one is cheap), and whether verification p separates correct from wrong
candidates. Keep `JEV_BATCH_SIZE` at 20 or below until then.

## Testing

Tests never call live APIs. `tests/mock_jev.py` adapts pg-jev's `test/mock_api.py` (same auth, `trigger422`,
`usage.input_tokens = len(body) // 4`, pg-jev's last-word rule for `condition` states) and lets a test install its
own scoring rule and queue failures. `tests/mock_llm.py` serves `/v1/chat/completions` with canned JSON chosen by
seed, plus a FIFO for repair calls. Both run as real HTTP servers on 127.0.0.1, so keep-alive reuse, retries and
concurrency limits are exercised end to end.
