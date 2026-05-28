"""Command-line entry point. argparse subcommands; each one is one function so it's
easy for a reviewer to read in any order.

Commands:
    init                Create the SQLite schema.
    ingest PATH         Load leads from .xlsx or .csv.
    preview [--batch]   Show which leads WOULD be called now (no side effects).
    queue               Snapshot of queue state: ready / cooling down / off-hours / next unlock.
    run [--batch ...]   Place calls and store results. Streams one line per call.
    results [--tier ...]   Top scored results, with filters.
    lead ID             Drill into one lead — contacts, attempts, latest result.
    reextract ATTEMPT_ID   Re-run extraction + scoring on a stored transcript.
    exclude LEAD_ID     Mark a lead as 'excluded' so it never gets called.
    requeue LEAD_ID     Reset a 'done' or 'excluded' lead back to 'new'.
    stats               Counts by status / outcome.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

from .config import load_config
from .db import connect, init_schema
from .ingest import ingest_file
from .orchestrator import PipelineRecord, reextract_attempt, run_batch
from .scheduler import preview_next_batch, queue_state


# US state name -> postal abbreviation, for compact per-call output. Anything not in the
# map falls back to the full state name (still readable, just wider).
_STATE_ABBR = {
    "Alabama":"AL","Alaska":"AK","Arizona":"AZ","Arkansas":"AR","California":"CA",
    "Colorado":"CO","Connecticut":"CT","Delaware":"DE","District of Columbia":"DC",
    "Florida":"FL","Georgia":"GA","Hawaii":"HI","Idaho":"ID","Illinois":"IL",
    "Indiana":"IN","Iowa":"IA","Kansas":"KS","Kentucky":"KY","Louisiana":"LA",
    "Maine":"ME","Maryland":"MD","Massachusetts":"MA","Michigan":"MI","Minnesota":"MN",
    "Mississippi":"MS","Missouri":"MO","Montana":"MT","Nebraska":"NE","Nevada":"NV",
    "New Hampshire":"NH","New Jersey":"NJ","New Mexico":"NM","New York":"NY",
    "North Carolina":"NC","North Dakota":"ND","Ohio":"OH","Oklahoma":"OK","Oregon":"OR",
    "Pennsylvania":"PA","Rhode Island":"RI","South Carolina":"SC","South Dakota":"SD",
    "Tennessee":"TN","Texas":"TX","Utah":"UT","Vermont":"VT","Virginia":"VA",
    "Washington":"WA","West Virginia":"WV","Wisconsin":"WI","Wyoming":"WY",
}


def _setup_logging(verbose: bool) -> None:
    level_name = os.getenv("MYSTERY_SHOP_LOG", "INFO" if verbose else "WARNING").upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _short_location(city: str | None, state: str | None) -> str:
    if not state:
        return city or "?"
    abbr = _STATE_ABBR.get(state, state)
    return f"{city or '?'}, {abbr}"


def _truncate(s: str | None, width: int) -> str:
    s = (s or "?")[:width]
    return s.ljust(width)


# ---------------- commands ----------------

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
        counters = ingest_file(conn, path)
    print(json.dumps(counters, indent=2))
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    cfg = load_config()
    with closing(connect(cfg.db_path)) as conn:
        init_schema(conn)
        rows = preview_next_batch(conn, cfg, args.batch)
    if not rows:
        print("No leads are callable right now. Try `mystery-shop queue` to see why.")
        return 0
    print(f"Next {len(rows)} lead{'s' if len(rows)!=1 else ''} that would be called:")
    print(f"{'#':>3}  {'phone':<15}  {'restaurant':<22}  {'location':<22}  attempts  timezone")
    print("-" * 100)
    for i, r in enumerate(rows, 1):
        print(f"{i:>3}  {r['phone']:<15}  "
              f"{_truncate(r['restaurant_name'], 22)}  "
              f"{_truncate(_short_location(r['city'], r['state']), 22)}  "
              f"{r['attempt_count']:>8}  {r['timezone']}")
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    cfg = load_config()
    with closing(connect(cfg.db_path)) as conn:
        init_schema(conn)
        state = queue_state(conn, cfg)
    if args.json:
        print(json.dumps(state, indent=2))
        return 0

    bh_lo, bh_hi = state["business_hours_local"]
    print(f"Queue state at {state['now_utc']} (business hours: {bh_lo:02d}:00–{bh_hi:02d}:00 local)")
    print(f"  Total leads: {state['leads_total']}")
    print(f"  By status:   {state['by_status']}")
    print()
    print(f"  Ready to call NOW:          {state['ready_now']}")
    print(f"  Off-hours (eligible but local time outside window): {state['off_hours_callable']}")
    print(f"  Cooling down (retry queued): {state['cooling_down']}")
    print(f"  No timezone (excluded):     {state['no_timezone']}")
    print(f"  Maxed attempts:             {state['maxed_attempts']}")
    if state["next_cooldown_unlock_at"]:
        print(f"  Next cooldown unlock at:    {state['next_cooldown_unlock_at']} (UTC)")
    return 0


def _print_call_line(idx: int, total: int, rec: PipelineRecord, conn: sqlite3.Connection) -> None:
    """One-line live progress entry. Looked-up restaurant/location from the lead row so we
    don't have to thread it through PipelineRecord."""
    lead = conn.execute(
        "SELECT restaurant_name, city, state FROM leads WHERE id = ?",
        (rec.lead_id,),
    ).fetchone()
    name = lead["restaurant_name"] if lead else None
    loc = _short_location(lead["city"] if lead else None, lead["state"] if lead else None)
    if rec.scoring:
        tier = rec.scoring.urgency_tier.upper()
        replac = rec.scoring.replaceability_score
        tier_disp = f"{tier:<5} r={replac:>4.1f}"
    else:
        tier_disp = "—" + " " * 10
    err_disp = f"  ERR: {rec.error}" if rec.error else ""
    print(f"[{idx:>2}/{total:<2}] {rec.phone:<15}  "
          f"{_truncate(name, 22)}  {_truncate(loc, 22)}  "
          f"→ {rec.outcome.value:<10}  {tier_disp}{err_disp}",
          flush=True)


def cmd_run(args: argparse.Namespace) -> int:
    # --realistic-delay sets the env var before load_config so the immutable Config picks it up.
    if args.realistic_delay is not None:
        os.environ["MOCK_CALL_DELAY_SECONDS"] = str(args.realistic_delay)
    cfg = load_config()
    with closing(connect(cfg.db_path)) as conn:
        init_schema(conn)
        if not args.quiet:
            print(f"Running batch of {args.batch} with provider={cfg.call_provider}"
                  f"{' (delay='+str(cfg.mock_call_delay_seconds)+'s)' if cfg.mock_call_delay_seconds else ''}",
                  flush=True)
        on_record = None if args.quiet else (lambda idx, total, rec: _print_call_line(idx, total, rec, conn))
        records = run_batch(conn, cfg, batch_size=args.batch, on_record=on_record)

    summary: dict[str, Any] = {
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

    if not records:
        print("\nNo leads callable right now. Run `mystery-shop queue` to see why "
              "(may be off-hours, cooldown, or empty queue).")
    else:
        if not args.quiet:
            print()  # blank line separating stream from summary
        print(json.dumps(summary, indent=2))
    return 0


def cmd_results(args: argparse.Namespace) -> int:
    cfg = load_config()
    sql = """SELECT r.id AS result_id, r.lead_id, r.replaceability_score,
                    r.scoring, r.extraction, r.created_at,
                    l.phone, l.restaurant_name, l.city, l.state,
                    ca.outcome, ca.transcript, ca.duration_seconds
             FROM call_results r
             JOIN leads l ON l.id = r.lead_id
             JOIN call_attempts ca ON ca.id = r.attempt_id"""
    where: list[str] = []
    params: list[Any] = []
    if args.tier:
        where.append("json_extract(r.scoring, '$.urgency_tier') = ?")
        params.append(args.tier)
    if args.outcome:
        where.append("ca.outcome = ?")
        params.append(args.outcome)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY r.replaceability_score DESC, r.created_at DESC LIMIT ?"
    params.append(args.limit)

    with closing(connect(cfg.db_path)) as conn:
        rows = conn.execute(sql, params).fetchall()
        total_results = conn.execute("SELECT COUNT(*) c FROM call_results").fetchone()["c"]

    out = []
    for row in rows:
        out.append({
            "result_id": row["result_id"],
            "lead_id": row["lead_id"],
            "restaurant": row["restaurant_name"],
            "phone": row["phone"],
            "location": _short_location(row["city"], row["state"]),
            "outcome": row["outcome"],
            "duration_seconds": row["duration_seconds"],
            "extraction": json.loads(row["extraction"]),
            "scoring": json.loads(row["scoring"]),
            "transcript": row["transcript"] if args.include_transcript else None,
        })
    print(json.dumps({
        "showing": len(out),
        "of_total": total_results,
        "filters": {k: v for k, v in (("tier", args.tier), ("outcome", args.outcome)) if v},
        "results": out,
    }, indent=2, default=str))
    return 0


def cmd_lead(args: argparse.Namespace) -> int:
    cfg = load_config()
    with closing(connect(cfg.db_path)) as conn:
        lead = conn.execute("SELECT * FROM leads WHERE id = ?", (args.lead_id,)).fetchone()
        if not lead:
            print(f"Lead {args.lead_id} not found.", file=sys.stderr)
            return 1
        contacts = conn.execute(
            "SELECT first_name, last_name, email FROM lead_contacts WHERE lead_id = ?",
            (args.lead_id,),
        ).fetchall()
        attempts = conn.execute(
            """SELECT id, started_at, outcome, provider, duration_seconds, transcript
               FROM call_attempts WHERE lead_id = ? ORDER BY id DESC""",
            (args.lead_id,),
        ).fetchall()
        latest_result = conn.execute(
            """SELECT extraction, scoring, replaceability_score, created_at
               FROM call_results WHERE lead_id = ? ORDER BY id DESC LIMIT 1""",
            (args.lead_id,),
        ).fetchone()

    out = {
        "lead": dict(lead),
        "contacts": [dict(c) for c in contacts],
        "attempts": [
            {**{k: a[k] for k in a.keys() if k != "transcript"},
             "transcript": a["transcript"] if args.include_transcripts else None}
            for a in attempts
        ],
        "latest_result": None,
    }
    if latest_result:
        out["latest_result"] = {
            "replaceability_score": latest_result["replaceability_score"],
            "created_at": latest_result["created_at"],
            "extraction": json.loads(latest_result["extraction"]),
            "scoring": json.loads(latest_result["scoring"]),
        }
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_reextract(args: argparse.Namespace) -> int:
    cfg = load_config()
    with closing(connect(cfg.db_path)) as conn:
        try:
            ext, sc = reextract_attempt(conn, cfg, args.attempt_id)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
    print(json.dumps({
        "attempt_id": args.attempt_id,
        "extraction": ext.to_dict(),
        "scoring": sc.to_dict(),
    }, indent=2, default=str))
    return 0


def cmd_exclude(args: argparse.Namespace) -> int:
    cfg = load_config()
    with closing(connect(cfg.db_path)) as conn:
        cur = conn.execute(
            "UPDATE leads SET status = 'excluded', next_eligible_at = NULL WHERE id = ?",
            (args.lead_id,),
        )
        if cur.rowcount == 0:
            print(f"Lead {args.lead_id} not found.", file=sys.stderr)
            return 1
    print(f"Lead {args.lead_id} excluded.")
    return 0


def cmd_requeue(args: argparse.Namespace) -> int:
    cfg = load_config()
    with closing(connect(cfg.db_path)) as conn:
        cur = conn.execute(
            """UPDATE leads
               SET status = 'new', next_eligible_at = NULL,
                   attempt_count = CASE WHEN ? THEN 0 ELSE attempt_count END
               WHERE id = ?""",
            (args.reset_attempts, args.lead_id),
        )
        if cur.rowcount == 0:
            print(f"Lead {args.lead_id} not found.", file=sys.stderr)
            return 1
    extra = " and attempts reset to 0" if args.reset_attempts else ""
    print(f"Lead {args.lead_id} requeued as 'new'{extra}.")
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


# ---------------- argparse ----------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mystery-shop")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="INFO-level logs from the pipeline (default WARNING).")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="Create the SQLite schema.")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("ingest", help="Load leads from .xlsx or .csv.")
    sp.add_argument("path", help="Path to leads file (.xlsx, .xlsm, .csv, .tsv)")
    sp.set_defaults(func=cmd_ingest)

    sp = sub.add_parser("preview", help="Show which leads would be called next (no side effects).")
    sp.add_argument("--batch", type=int, default=10)
    sp.set_defaults(func=cmd_preview)

    sp = sub.add_parser("queue", help="Snapshot of queue state right now.")
    sp.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    sp.set_defaults(func=cmd_queue)

    sp = sub.add_parser("run", help="Place calls and store results.")
    sp.add_argument("--batch", type=int, default=10)
    sp.add_argument("--quiet", action="store_true", help="Suppress per-call streaming output.")
    sp.add_argument("--realistic-delay", type=float, default=None, metavar="SECONDS",
                    help="Mock provider sleeps SECONDS (with jitter) per call to simulate real timing. Try 5 for demos.")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("results", help="Print scored results.")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--tier", choices=["hot", "warm", "cold", "skip"])
    sp.add_argument("--outcome", choices=["answered", "voicemail", "no_answer", "busy", "failed"])
    sp.add_argument("--include-transcript", action="store_true")
    sp.set_defaults(func=cmd_results)

    sp = sub.add_parser("lead", help="Drill into one lead — contacts, attempts, latest result.")
    sp.add_argument("lead_id", type=int)
    sp.add_argument("--include-transcripts", action="store_true")
    sp.set_defaults(func=cmd_lead)

    sp = sub.add_parser("reextract", help="Re-run extraction+scoring on a stored transcript.")
    sp.add_argument("attempt_id", type=int)
    sp.set_defaults(func=cmd_reextract)

    sp = sub.add_parser("exclude", help="Mark a lead as 'excluded' so it never gets called.")
    sp.add_argument("lead_id", type=int)
    sp.set_defaults(func=cmd_exclude)

    sp = sub.add_parser("requeue", help="Reset a 'done' or 'excluded' lead back to 'new'.")
    sp.add_argument("lead_id", type=int)
    sp.add_argument("--reset-attempts", action="store_true",
                    help="Also reset attempt_count to 0 (otherwise retains it).")
    sp.set_defaults(func=cmd_requeue)

    sp = sub.add_parser("stats", help="Counts by status/outcome.")
    sp.set_defaults(func=cmd_stats)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
