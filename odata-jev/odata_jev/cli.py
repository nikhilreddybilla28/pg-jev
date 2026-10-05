"""odata-jev "question" --metadata file.xml|file.json

Settings come from environment variables (TYPESAFE_API_KEY, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, ...).
Exit codes: 0 ok, 1 no valid query, 2 bad input or settings, 3 API error.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from . import __version__
from .errors import ConfigError, JevError, LLMError, MetadataError, NoValidQueryError
from .pipeline import Result, Session
from .settings import Settings


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="odata-jev",
        description="Turn a plain-language question into an OData query. An LLM writes candidates; TypeSafe's Jev "
        "picks the entity set, prunes properties and verifies the result.",
    )
    p.add_argument("question", help="the question, in quotes")
    p.add_argument("--metadata", "-m", required=True, help="$metadata EDMX (.xml/.edmx) or JSON tool details (.json)")
    p.add_argument(
        "--odata-version", choices=["v2", "v4"], help="default: what the metadata declares, then ODATA_VERSION"
    )
    p.add_argument("--candidates", "-n", type=int, help="LLM candidates to generate (default 3)")
    p.add_argument("--service-url", help="service root, if the metadata does not carry it")
    p.add_argument("--today", help="date for relative dates, YYYY-MM-DD (default: today)")
    p.add_argument("--json", action="store_true", help="print the full result as JSON")
    p.add_argument("--verbose", "-v", action="store_true", help="log every Jev and LLM call to stderr")
    p.add_argument("--version", action="version", version=f"odata-jev {__version__}")
    return p


def _cost_line(stats: dict[str, Any]) -> str:
    j, m = stats["jev"]["total"], stats["llm"]["total"]
    jev = f"Jev {j['requests']} requests, {j['input_tokens']:,} input tokens ≈ ${j['estimated_cost_usd']:.6f}"
    llm_cost = m["estimated_cost_usd"]
    llm = f"LLM {m['requests']} requests, {m['input_tokens']:,} + {m['output_tokens']:,} tokens " + (
        "(cost unknown: set LLM_PRICE_*_PER_MTOK)" if llm_cost is None else f"≈ ${llm_cost:.6f}"
    )
    return f"{jev}; {llm}; {stats['elapsed_ms']:.0f} ms"


def format_result(r: Result) -> str:
    agree = next((c.votes for c in r.candidates if c.query == r.query), 1)
    scored = [s for s in r.entity_sets if s.probability is not None]
    sets = r.entity_set
    if scored:
        sets += " (" + ", ".join(f"{s.name} {s.probability:.2f}" for s in scored[:3]) + ")"
    lines = [
        f"GET {r.query_url}",
        "",
        f"  entity set   {sets}",
        f"  confidence   {r.confidence:.2f} from Jev ({agree} of {len(r.candidates)} candidates agree)",
        f"  explanation  {r.explanation}",
    ]
    if r.residual_condition:
        lines.append(f"  residual     {r.residual_condition}")
        lines.append("               run the query, then filter its records with odata_jev.filter_rows(rows, residual)")
    for i, (k, v) in enumerate(r.parts.items()):
        lines.append(f"  {'options' if i == 0 else '':<12} {k} = {v}")
    for w in r.warnings:
        lines.append(f"  warning      {w}")
    lines.append(f"  cost         {_cost_line(r.stats)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.verbose:
        logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
        logging.getLogger("httpx").setLevel(logging.WARNING)  # our own per-call lines carry tokens and cost
    try:
        settings = Settings.from_env()
        with Session(settings) as session:
            result = session.text_to_odata(
                args.question,
                args.metadata,
                args.odata_version,
                args.candidates,
                today=args.today,
                service_url=args.service_url,
            )
    except NoValidQueryError as e:
        print(f"odata-jev: {e}", file=sys.stderr)
        for c in e.candidates:
            print(f"  candidate {c.index}: {c.entity_set} {json.dumps(c.query_options)}", file=sys.stderr)
            for i in c.issues:
                if i.severity == "error":
                    print(f"    - {i}", file=sys.stderr)
        return 1
    except (ConfigError, MetadataError, ValueError) as e:
        print(f"odata-jev: {e}", file=sys.stderr)
        return 2
    except (JevError, LLMError) as e:
        print(f"odata-jev: {e}", file=sys.stderr)
        return 3
    print(result.model_dump_json(indent=2) if args.json else format_result(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
