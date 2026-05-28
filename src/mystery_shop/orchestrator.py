"""End-to-end pipeline: pick lead → call → store attempt → extract → score → store result.

Designed so each step is a pure function over the previous step's output. That's why we
can swap providers and extractors via config without touching this file."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .agent_script import opening_line, system_prompt
from .config import Config
from .db import dumps
from .extractor import Extraction, build_extractor
from .providers.base import CallOutcome, CallProvider, CallRequest, CallResult
from .providers.factory import build_provider
from .scheduler import apply_retry_policy, claim_next_batch
from .scorer import Score, score


@dataclass
class PipelineRecord:
    lead_id: int
    phone: str
    outcome: CallOutcome
    attempt_id: int
    result_id: int | None
    extraction: Extraction | None
    scoring: Score | None
    transcript: str
    duration_seconds: float


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


def run_batch(
    conn: sqlite3.Connection,
    cfg: Config,
    batch_size: int,
    provider: CallProvider | None = None,
) -> list[PipelineRecord]:
    """Process up to `batch_size` callable leads end-to-end. Returns one record per call."""
    provider = provider or build_provider(cfg)
    extractor = build_extractor(cfg)
    leads = claim_next_batch(conn, cfg, batch_size)
    out: list[PipelineRecord] = []

    for lead in leads:
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

        out.append(PipelineRecord(
            lead_id=lead["id"], phone=lead["phone"], outcome=call.outcome,
            attempt_id=attempt_id, result_id=result_id,
            extraction=ext, scoring=sc,
            transcript=call.transcript, duration_seconds=call.duration_seconds,
        ))

    return out
