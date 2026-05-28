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

import logging
import sqlite3
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Config
from .db import transaction
from .providers.base import CallOutcome

logger = logging.getLogger(__name__)


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


def _over_fetch_factor(limit: int) -> int:
    """How many rows to pull from SQL relative to the requested limit. Larger when limit is
    small so we have enough headroom to filter out off-hours leads in Python without going
    back to the DB. Capped so we don't accidentally pull tens of thousands of rows."""
    return min(50, max(5, limit * 5))


def claim_next_batch(conn: sqlite3.Connection, cfg: Config, limit: int) -> list[sqlite3.Row]:
    """Atomically pick the next N callable leads and mark them in_progress.

    We filter by business hours in Python (the DB can't know about timezones), so we
    over-fetch then trim. Skip reasons are aggregated and logged so operators can tell
    'we returned 0' apart from 'we returned 0 because every callable lead is on the
    east coast and it's currently 1am there'."""
    now_utc_iso = _now_utc().isoformat()
    candidates: list[sqlite3.Row] = []
    skip_counts: dict[str, int] = {"outside_business_hours": 0}

    with transaction(conn):
        # Count what's definitively unavailable so the operator sees the shape of the queue.
        unavailable = conn.execute(
            """SELECT
                 SUM(CASE WHEN timezone IS NULL THEN 1 ELSE 0 END) AS no_timezone,
                 SUM(CASE WHEN attempt_count >= ? THEN 1 ELSE 0 END) AS maxed_attempts,
                 SUM(CASE WHEN next_eligible_at IS NOT NULL AND next_eligible_at > ?
                          THEN 1 ELSE 0 END) AS cooling_down
               FROM leads
               WHERE status IN ('new', 'in_progress')""",
            (cfg.max_attempts_per_lead, now_utc_iso),
        ).fetchone()
        skip_counts["no_timezone"] = unavailable["no_timezone"] or 0
        skip_counts["maxed_attempts"] = unavailable["maxed_attempts"] or 0
        skip_counts["cooling_down"] = unavailable["cooling_down"] or 0

        rows = conn.execute(
            """SELECT * FROM leads
               WHERE status IN ('new', 'in_progress')
                 AND attempt_count < ?
                 AND (next_eligible_at IS NULL OR next_eligible_at <= ?)
                 AND timezone IS NOT NULL
               ORDER BY attempt_count ASC, id ASC
               LIMIT ?""",
            (cfg.max_attempts_per_lead, now_utc_iso, _over_fetch_factor(limit)),
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
            else:
                skip_counts["outside_business_hours"] += 1

    if len(candidates) < limit:
        logger.info(
            "claim_next_batch: returned %d of %d requested. Skip counts: %s",
            len(candidates), limit,
            {k: v for k, v in skip_counts.items() if v},
        )
    else:
        logger.debug("claim_next_batch: returned %d leads", len(candidates))
    return candidates


def preview_next_batch(conn: sqlite3.Connection, cfg: Config, limit: int) -> list[sqlite3.Row]:
    """Same selection logic as claim_next_batch but READ-ONLY — does not mark in_progress.
    Used by `mystery-shop preview` so reviewers can see who would be called before any
    side effects happen."""
    now_utc_iso = _now_utc().isoformat()
    rows = conn.execute(
        """SELECT * FROM leads
           WHERE status IN ('new', 'in_progress')
             AND attempt_count < ?
             AND (next_eligible_at IS NULL OR next_eligible_at <= ?)
             AND timezone IS NOT NULL
           ORDER BY attempt_count ASC, id ASC
           LIMIT ?""",
        (cfg.max_attempts_per_lead, now_utc_iso, _over_fetch_factor(limit)),
    ).fetchall()
    out: list[sqlite3.Row] = []
    for row in rows:
        if len(out) >= limit:
            break
        if is_within_business_hours(row["timezone"], cfg):
            out.append(row)
    return out


def queue_state(conn: sqlite3.Connection, cfg: Config) -> dict:
    """Snapshot of the queue right now. Used by `mystery-shop queue`.

    Returns:
      - leads_total / by_status counts
      - ready_now: leads that would be called this minute
      - off_hours_callable: leads eligible by attempt/cooldown but outside local business hours
      - cooling_down: leads with next_eligible_at in the future
      - no_timezone: excluded due to missing tz
      - next_unlock_at: the earliest next_eligible_at in the future (when something new opens up)
    """
    now = _now_utc()
    now_iso = now.isoformat()

    by_status = {
        r["status"]: r["c"]
        for r in conn.execute("SELECT status, COUNT(*) c FROM leads GROUP BY status").fetchall()
    }
    total = sum(by_status.values())

    # Compute partition for new + in_progress leads (the actionable bucket).
    actionable = conn.execute(
        """SELECT id, timezone, next_eligible_at, attempt_count
           FROM leads
           WHERE status IN ('new', 'in_progress')""",
    ).fetchall()

    ready_now = off_hours_callable = cooling_down = no_timezone = maxed_attempts = 0
    next_unlock: datetime | None = None
    for row in actionable:
        if not row["timezone"]:
            no_timezone += 1
            continue
        if row["attempt_count"] >= cfg.max_attempts_per_lead:
            maxed_attempts += 1
            continue
        if row["next_eligible_at"] and row["next_eligible_at"] > now_iso:
            cooling_down += 1
            try:
                t = datetime.fromisoformat(row["next_eligible_at"])
                if next_unlock is None or t < next_unlock:
                    next_unlock = t
            except ValueError:
                pass
            continue
        if is_within_business_hours(row["timezone"], cfg):
            ready_now += 1
        else:
            off_hours_callable += 1

    return {
        "now_utc": now_iso,
        "leads_total": total,
        "by_status": by_status,
        "ready_now": ready_now,
        "off_hours_callable": off_hours_callable,
        "cooling_down": cooling_down,
        "no_timezone": no_timezone,
        "maxed_attempts": maxed_attempts,
        "next_cooldown_unlock_at": next_unlock.isoformat() if next_unlock else None,
        "business_hours_local": cfg.business_hours_local,
    }


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
