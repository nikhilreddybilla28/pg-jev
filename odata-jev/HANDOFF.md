# Handoff: testing odata-jev against real SAP services

For the agent (or person) picking this up on a machine that can reach SAP, TypeSafe and an LLM.

## Where things stand

- Code: `odata-jev/` in `nikhilreddybilla28/pg-jev`, branch `claude/gracious-faraday-f0d4xq`. Self-contained; it
  does not depend on the PostgreSQL extension around it.
- Tests: 226, all against `tests/mock_jev.py` and `tests/mock_llm.py`. Nothing has run against a live TypeSafe,
  LLM or SAP endpoint yet.
- The validator follows the OData 4.01 ABNF, the Part 2 precedence table and the V2 URI conventions
  (`tests/test_spec_conformance.py`). Several V2 rules are assumptions about SAP Gateway that have not been checked
  on a real system:
  - `datetime'…'` must have a time part;
  - `Edm.DateTimeOffset` needs `datetimeoffset'…'`;
  - `substringof(...)` is accepted without `eq true`;
  - no spaces after commas in `$select`.
- Not measured: the four Jev instructions (`SELECT_Q`, `FIELD_Q`, `VERIFY_Q`, `ROW_Q` in `pipeline.py`), the prompt
  and the cheat sheets in `versions.py`, and the thresholds (`selection_margin` 0.1, `property_threshold` 0.3,
  `min_properties` 8, `max_properties` 60). See DESIGN.md, "Not measured yet".
- Out of scope so far: odata-jev builds the query but never calls the OData service; `$apply`/`$compute`, type-cast
  segments, `$root` and parameter aliases are rejected; OData V3 metadata is treated as V2.

## Set up

```bash
git clone https://github.com/nikhilreddybilla28/pg-jev && cd pg-jev && git checkout claude/gracious-faraday-f0d4xq
cd odata-jev
python3.11 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
cp .env.example .env        # fill in TYPESAFE_API_KEY, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL (git-ignored)
pytest && ruff check .      # must stay green; these never touch live services
```

To move only this package into its own repository with its history:
`git subtree split --prefix=odata-jev -b odata-jev-only`, then push that branch to the new remote.

SAP connection details (host, client, user, password or OAuth) belong in `.env` too, for example
`SAP_BASE_URL`, `SAP_CLIENT`, `SAP_USER`, `SAP_PASSWORD`. odata-jev itself does not read them. The test harness
below does.

## Plan, in order

1. **Metadata.** Download `$metadata` of the target services (V2: `/sap/opu/odata/sap/<SERVICE>/$metadata`,
   V4 RAP: `/sap/opu/odata4/sap/<service_binding>/srvd_a2x/sap/<service>/0001/$metadata`, both with
   `?sap-client=<client>`). Load each with `load_tools`. Check entity set counts, labels (`sap:label`,
   `Common.Label`), filterable/sortable/searchable flags and navigation bindings against what the Gateway client
   (`/IWFND/GW_CLIENT`) shows. Save the files under `eval/metadata/`.
2. **Validator vs Gateway.** Write 40–60 hand-made queries per version: correct ones, and ones that break a single
   rule each (the cases in `tests/test_spec_conformance.py` are a good start). Send each with GET and
   `Accept: application/json`. Every query the validator accepts must return 200. Every query it rejects should
   fail on SAP too, or the rule is too strict. Fix the validator where it disagrees with the server, and add each
   case to the tests.
3. **End-to-end evaluation.** Write 30–50 real questions per service, each with an expected answer: the entity set,
   the conditions that must appear, and ideally the expected record keys taken from SAP. Run `text_to_odata`,
   execute the returned `query_url`, and record:
   - share of questions with a valid query, and share that return HTTP 200;
   - result match against the expected keys;
   - selection accuracy (`entity_sets`), and pruning recall: was every property the expected answer needs kept?
   - calibration: accuracy per `confidence` bucket (0–0.5, 0.5–0.8, 0.8–1);
   - cost and latency from `stats`.
   Keep the harness in `eval/` (a script plus a JSONL file of questions); it is not part of the package.
4. **Tune only with numbers.** Change thresholds, the Jev instructions or the prompt one at a time and rerun the
   evaluation. Keep `JEV_BATCH_SIZE` at 20 or below (pg-jev measured accuracy dropping above that). Record every
   before/after in `eval/RESULTS.md`, like pg-jev's "Why 20 rows per request" section.
5. **Residual conditions.** On a text field (order notes, ticket text), label 50–100 records by hand and compare
   `judge_rows` probabilities against the labels to pick a threshold.

## Rules

- GET only. Never send POST/PUT/PATCH/DELETE or `$batch` to SAP. Prefer a QA/sandbox client over production.
- Questions and pruned metadata go to the LLM and to TypeSafe; `judge_rows` sends record contents to TypeSafe.
  Confirm that is allowed for the data before running it. Use `fields=` to send only what the judgment needs.
- Never print, log or commit secrets; `.env` stays git-ignored.
- Tests under `tests/` stay mock-only. Live checks go in `eval/` or behind a pytest marker that is skipped unless
  an environment variable enables it.
- Ask before adding any dependency beyond httpx, pydantic, lxml, pytest and ruff.
