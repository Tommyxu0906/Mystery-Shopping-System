"""Decides which leads are callable RIGHT NOW.

Rules (all easy to extend):
  - Lead status must be 'new' or 'in_progress'.
  - It must currently be inside business hours (11am-8pm by default) in the lead's local timezone.
  - Lead's next_eligible_at must be in the past (used for retry cooldowns).
  - We never dispatch two attempts to the same phone in the same batch (the brief's
    "don't call the same number twice in a row" rule). With our schema this is implicit —
    one lead = one phone — but the LIMIT/ordering guarantees fairness across cities.
  - attempt_count < max_attempts_per_lead (default 3).

Retry policy after an attempt:
  - answered → status='done', no more attempts.
  - voicemail → status='done' (we got the data point we wanted).
  - no_answer → cooldown 2h, retry up to 3 attempts total.
  - busy → cooldown 30m, retry up to 3 attempts total.
  - failed → cooldown 24h, retry once more then give up.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Config
from .db import transaction
from .providers.base import CallOutcome


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def is_within_business_hours(tz_name: str | None, cfg: Config, now: datetime | None = None) -> bool:
    """True if local clock in tz is between cfg.business_hours_local[0] and [1] (24h)."""
    if not tz_name:
        return False  # don't call anything without a known timezone
    now = now or _now_utc()
    try:
        local = now.astimezone(ZoneInfo(tz_name))
    except Exception:
        return False
    start_h, end_h = cfg.business_hours_local
    return time(start_h, 0) <= local.time() <= time(end_h, 0)


def claim_next_batch(conn: sqlite3.Connection, cfg: Config, limit: int) -> list[sqlite3.Row]:
    """Atomically pick the next N callable leads and mark them in_progress."""
    now_utc_iso = _now_utc().isoformat()
    candidates: list[sqlite3.Row] = []
    with transaction(conn):
        rows = conn.execute(
            """SELECT * FROM leads
               WHERE status IN ('new', 'in_progress')
                 AND attempt_count < ?
                 AND (next_eligible_at IS NULL OR next_eligible_at <= ?)
                 AND timezone IS NOT NULL
               ORDER BY attempt_count ASC, id ASC
               LIMIT ?""",
            (cfg.max_attempts_per_lead, now_utc_iso, limit * 5),
            # Over-fetch because we filter by business hours in Python (which the DB doesn't know about).
        ).fetchall()
        for row in rows:
            if len(candidates) >= limit:
                break
            if is_within_business_hours(row["timezone"], cfg):
                candidates.append(row)
                conn.execute(
                    "UPDATE leads SET status = 'in_progress', last_attempt_at = ? WHERE id = ?",
                    (now_utc_iso, row["id"]),
                )
    return candidates


def apply_retry_policy(
    conn: sqlite3.Connection, cfg: Config, lead_id: int, outcome: CallOutcome
) -> None:
    """Update the lead row for what should happen next given this attempt's outcome."""
    now = _now_utc()
    with transaction(conn):
        lead = conn.execute(
            "SELECT attempt_count FROM leads WHERE id = ?", (lead_id,)
        ).fetchone()
        new_attempts = (lead["attempt_count"] if lead else 0) + 1

        if outcome in (CallOutcome.ANSWERED, CallOutcome.VOICEMAIL):
            status, next_eligible = "done", None
        elif new_attempts >= cfg.max_attempts_per_lead:
            status, next_eligible = "done", None
        elif outcome == CallOutcome.NO_ANSWER:
            status = "new"
            next_eligible = (now + timedelta(minutes=cfg.retry_after_no_answer_min)).isoformat()
        elif outcome == CallOutcome.BUSY:
            status = "new"
            next_eligible = (now + timedelta(minutes=cfg.retry_after_busy_min)).isoformat()
        elif outcome == CallOutcome.FAILED:
            status = "new"
            next_eligible = (now + timedelta(hours=24)).isoformat()
        else:
            status, next_eligible = "new", None

        conn.execute(
            "UPDATE leads SET status = ?, attempt_count = ?, next_eligible_at = ? WHERE id = ?",
            (status, new_attempts, next_eligible, lead_id),
        )
