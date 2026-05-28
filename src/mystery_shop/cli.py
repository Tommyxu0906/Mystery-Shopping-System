"""Command-line entry point. One file, argparse — no need for click for this size."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from contextlib import closing
from pathlib import Path

from .config import load_config
from .db import connect, init_schema
from .ingest import ingest_xlsx
from .orchestrator import run_batch


def _setup_logging(verbose: bool) -> None:
    """Honour MYSTERY_SHOP_LOG (env) or --verbose. Default level INFO so the user sees the
    orchestrator's batch progress without opting in. Quiet by default in tests."""
    level_name = os.getenv("MYSTERY_SHOP_LOG", "INFO" if verbose else "WARNING").upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_init(args: argparse.Namespace) -> int:
    cfg = load_config()
    with closing(connect(cfg.db_path)) as conn:
        init_schema(conn)
    print(f"Initialized schema at {cfg.db_path}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    cfg = load_config()
    path = Path(args.path).resolve()
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        return 1
    with closing(connect(cfg.db_path)) as conn:
        init_schema(conn)
        counters = ingest_xlsx(conn, path)
    print(json.dumps(counters, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config()
    with closing(connect(cfg.db_path)) as conn:
        init_schema(conn)
        records = run_batch(conn, cfg, batch_size=args.batch)
    summary = {
        "calls_placed": len(records),
        "outcomes": {},
        "tiers": {},
        "errors": 0,
    }
    for r in records:
        summary["outcomes"][r.outcome.value] = summary["outcomes"].get(r.outcome.value, 0) + 1
        if r.scoring:
            t = r.scoring.urgency_tier
            summary["tiers"][t] = summary["tiers"].get(t, 0) + 1
        if r.error:
            summary["errors"] += 1
    print(json.dumps(summary, indent=2))
    return 0


def cmd_results(args: argparse.Namespace) -> int:
    cfg = load_config()
    with closing(connect(cfg.db_path)) as conn:
        rows = conn.execute(
            """SELECT
                 r.id AS result_id,
                 r.replaceability_score,
                 r.scoring,
                 r.extraction,
                 r.created_at,
                 l.phone, l.restaurant_name, l.city, l.state,
                 ca.outcome, ca.transcript, ca.duration_seconds
               FROM call_results r
               JOIN leads l ON l.id = r.lead_id
               JOIN call_attempts ca ON ca.id = r.attempt_id
               ORDER BY r.replaceability_score DESC, r.created_at DESC
               LIMIT ?""",
            (args.limit,),
        ).fetchall()
    out = []
    for row in rows:
        out.append({
            "result_id": row["result_id"],
            "restaurant": row["restaurant_name"],
            "phone": row["phone"],
            "location": f"{row['city']}, {row['state']}" if row["city"] else row["state"],
            "outcome": row["outcome"],
            "duration_seconds": row["duration_seconds"],
            "extraction": json.loads(row["extraction"]),
            "scoring": json.loads(row["scoring"]),
            "transcript": row["transcript"] if args.include_transcript else None,
        })
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    cfg = load_config()
    with closing(connect(cfg.db_path)) as conn:
        lead_total = conn.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
        by_status = conn.execute("SELECT status, COUNT(*) c FROM leads GROUP BY status").fetchall()
        attempts = conn.execute("SELECT COUNT(*) c FROM call_attempts").fetchone()["c"]
        by_outcome = conn.execute(
            "SELECT outcome, COUNT(*) c FROM call_attempts GROUP BY outcome"
        ).fetchall()
        results = conn.execute("SELECT COUNT(*) c FROM call_results").fetchone()["c"]
    print(json.dumps({
        "leads_total": lead_total,
        "leads_by_status": {r["status"]: r["c"] for r in by_status},
        "attempts_total": attempts,
        "attempts_by_outcome": {r["outcome"]: r["c"] for r in by_outcome},
        "results_total": results,
    }, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mystery-shop")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Enable INFO-level logging from the pipeline (default WARNING).")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="Create the SQLite schema.")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("ingest", help="Load leads from an .xlsx file.")
    sp.add_argument("path", help="Path to leads.xlsx")
    sp.set_defaults(func=cmd_ingest)

    sp = sub.add_parser("run", help="Place calls and store results.")
    sp.add_argument("--batch", type=int, default=10, help="Number of leads to process this run.")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("results", help="Print scored results, highest replaceability first.")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--include-transcript", action="store_true")
    sp.set_defaults(func=cmd_results)

    sp = sub.add_parser("stats", help="Counts by status/outcome.")
    sp.set_defaults(func=cmd_stats)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
