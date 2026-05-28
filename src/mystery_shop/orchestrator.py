"""End-to-end pipeline: pick lead → call → store attempt → extract → score → store result.

Designed so each step is a pure function over the previous step's output. That's why we
can swap providers and extractors via config without touching this file.

Error handling: any unexpected exception in a single lead's processing is caught at the
batch boundary so one bad call doesn't poison the entire batch. The failing lead is
recorded as a 'failed' attempt and run through the standard retry policy, which means
it gets a cooldown and can be retried later — never left stuck in 'in_progress'."""
from __future__ import annotations

import logging
import sqlite3
import traceback
from dataclasses import dataclass

from .agent_script import opening_line, system_prompt
from .config import Config
from .db import dumps
from .extractor import Extraction, build_extractor
from .providers.base import CallOutcome, CallProvider, CallRequest, CallResult
from .providers.factory import build_provider
from .scheduler import apply_retry_policy, claim_next_batch
from .scorer import Score, score

logger = logging.getLogger(__name__)


@dataclass
class PipelineRecord:
    lead_id: int
    phone: str
    outcome: CallOutcome
    attempt_id: int | None
    result_id: int | None
    extraction: Extraction | None
    scoring: Score | None
    transcript: str
    duration_seconds: float
    error: str | None = None  # set when the pipeline failed for this lead


def _record_attempt(conn: sqlite3.Connection, lead_id: int, call: CallResult) -> int:
    cur = conn.execute(
        """INSERT INTO call_attempts
            (lead_id, ended_at, outcome, provider, provider_call_id, duration_seconds, transcript, raw_metadata)
           VALUES (?, datetime('now'), ?, ?, ?, ?, ?, ?)""",
        (
            lead_id, call.outcome.value, call.provider, call.provider_call_id,
            call.duration_seconds, call.transcript, dumps(call.raw_metadata),
        ),
    )
    return cur.lastrowid


def _record_result(conn: sqlite3.Connection, attempt_id: int, lead_id: int, ext: Extraction, sc: Score) -> int:
    cur = conn.execute(
        """INSERT INTO call_results
            (attempt_id, lead_id, extraction, scoring, overall_score, replaceability_score)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            attempt_id, lead_id, dumps(ext.to_dict()), dumps(sc.to_dict()),
            sc.replaceability_score, sc.replaceability_score,
        ),
    )
    return cur.lastrowid


def _record_synthetic_failure(conn: sqlite3.Connection, lead_id: int, provider_name: str, exc: Exception) -> int:
    """When the provider raised before returning a CallResult, we still want a row so the
    audit trail isn't a black hole. Mark it as a failed attempt with the exception captured."""
    cur = conn.execute(
        """INSERT INTO call_attempts
            (lead_id, ended_at, outcome, provider, duration_seconds, transcript, raw_metadata)
           VALUES (?, datetime('now'), 'failed', ?, 0, ?, ?)""",
        (
            lead_id, provider_name,
            f"[pipeline exception before call completed: {type(exc).__name__}]",
            dumps({"error_type": type(exc).__name__, "error": str(exc), "traceback": traceback.format_exc()}),
        ),
    )
    return cur.lastrowid


def _process_lead(
    conn: sqlite3.Connection,
    cfg: Config,
    provider: CallProvider,
    extractor,
    lead: sqlite3.Row,
) -> PipelineRecord:
    """Happy path for one lead. The caller is responsible for try/except."""
    req = CallRequest(
        lead_id=lead["id"],
        phone=lead["phone"],
        restaurant_name=lead["restaurant_name"],
        system_prompt=system_prompt(),
        opening_line=opening_line(),
    )
    call = provider.place_call(req)
    attempt_id = _record_attempt(conn, lead["id"], call)

    ext = extractor.extract(call.transcript, call.outcome)
    sc = score(ext, call.outcome)
    result_id = _record_result(conn, attempt_id, lead["id"], ext, sc)

    apply_retry_policy(conn, cfg, lead["id"], call.outcome)

    return PipelineRecord(
        lead_id=lead["id"], phone=lead["phone"], outcome=call.outcome,
        attempt_id=attempt_id, result_id=result_id,
        extraction=ext, scoring=sc,
        transcript=call.transcript, duration_seconds=call.duration_seconds,
    )


def run_batch(
    conn: sqlite3.Connection,
    cfg: Config,
    batch_size: int,
    provider: CallProvider | None = None,
) -> list[PipelineRecord]:
    """Process up to `batch_size` callable leads end-to-end. Returns one record per lead.

    Failures on individual leads do not abort the batch. A lead that errored is converted
    into a synthetic 'failed' attempt and handed back to the retry policy, so it will be
    re-queued after the standard failed-call cooldown. This means a transient provider
    outage degrades gracefully instead of stalling the queue."""
    provider = provider or build_provider(cfg)
    extractor = build_extractor(cfg)
    leads = claim_next_batch(conn, cfg, batch_size)
    if not leads:
        logger.info("run_batch: no callable leads right now")
        return []

    logger.info("run_batch: processing %d leads with provider=%s extractor=%s",
                len(leads), provider.name, getattr(extractor, "name", "?"))

    out: list[PipelineRecord] = []
    for lead in leads:
        try:
            out.append(_process_lead(conn, cfg, provider, extractor, lead))
        except Exception as exc:
            logger.exception("Lead %s failed mid-pipeline; recording synthetic failure", lead["id"])
            try:
                attempt_id = _record_synthetic_failure(conn, lead["id"], provider.name, exc)
                apply_retry_policy(conn, cfg, lead["id"], CallOutcome.FAILED)
            except Exception:
                # If even the DB write fails, we can't do much except log and move on.
                logger.exception("Could not record synthetic failure for lead %s", lead["id"])
                attempt_id = None
            out.append(PipelineRecord(
                lead_id=lead["id"], phone=lead["phone"], outcome=CallOutcome.FAILED,
                attempt_id=attempt_id, result_id=None,
                extraction=None, scoring=None,
                transcript="", duration_seconds=0.0,
                error=f"{type(exc).__name__}: {exc}",
            ))

    return out
