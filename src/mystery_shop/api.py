"""Minimal read API for the SDR-facing view.

Run with:  uvicorn mystery_shop.api:app --reload

Endpoints:
    GET /healthz
    GET /leads?limit=&status=&state=
    GET /results?tier=hot|warm|cold|skip&limit=
    GET /results/{result_id}
    GET /stats
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from .config import load_config
from .db import connect, init_schema

app = FastAPI(title="Mystery Shop API", version="0.1.0")
_cfg = load_config()


def _conn():
    conn = connect(_cfg.db_path)
    init_schema(conn)
    return conn


def _result_row(row) -> dict[str, Any]:
    extraction = json.loads(row["extraction"])
    scoring = json.loads(row["scoring"])
    return {
        "result_id": row["result_id"],
        "restaurant": row["restaurant_name"],
        "phone": row["phone"],
        "location": f"{row['city']}, {row['state']}" if row["city"] else row["state"],
        "outcome": row["outcome"],
        "replaceability_score": row["replaceability_score"],
        "urgency_tier": scoring["urgency_tier"],
        "sdr_one_liner": scoring["sdr_one_liner"],
        "scoring": scoring,
        "extraction": extraction,
        "created_at": row["created_at"],
    }


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/stats")
def stats() -> dict[str, Any]:
    with _conn() as conn:
        lead_total = conn.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
        by_status = conn.execute("SELECT status, COUNT(*) c FROM leads GROUP BY status").fetchall()
        attempts = conn.execute("SELECT COUNT(*) c FROM call_attempts").fetchone()["c"]
        by_outcome = conn.execute("SELECT outcome, COUNT(*) c FROM call_attempts GROUP BY outcome").fetchall()
        by_tier = conn.execute(
            """SELECT json_extract(scoring, '$.urgency_tier') t, COUNT(*) c
               FROM call_results GROUP BY t"""
        ).fetchall()
    return {
        "leads_total": lead_total,
        "leads_by_status": {r["status"]: r["c"] for r in by_status},
        "attempts_total": attempts,
        "attempts_by_outcome": {r["outcome"]: r["c"] for r in by_outcome},
        "results_by_tier": {r["t"]: r["c"] for r in by_tier},
    }


@app.get("/leads")
def list_leads(
    limit: int = Query(50, le=500),
    status: str | None = None,
    state: str | None = None,
) -> list[dict[str, Any]]:
    where, params = ["1=1"], []
    if status:
        where.append("status = ?"); params.append(status)
    if state:
        where.append("state = ?"); params.append(state)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT id, phone, restaurant_name, city, state, timezone, status, attempt_count, "
            f"last_attempt_at, next_eligible_at FROM leads WHERE {' AND '.join(where)} "
            f"ORDER BY id LIMIT ?",
            (*params, limit),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/results")
def list_results(
    tier: str | None = Query(None, pattern="^(hot|warm|cold|skip)$"),
    limit: int = Query(50, le=500),
) -> list[dict[str, Any]]:
    sql = """SELECT r.id AS result_id, r.replaceability_score, r.scoring, r.extraction, r.created_at,
                    l.phone, l.restaurant_name, l.city, l.state,
                    ca.outcome
             FROM call_results r
             JOIN leads l ON l.id = r.lead_id
             JOIN call_attempts ca ON ca.id = r.attempt_id"""
    params: list[Any] = []
    if tier:
        sql += " WHERE json_extract(r.scoring, '$.urgency_tier') = ?"
        params.append(tier)
    sql += " ORDER BY r.replaceability_score DESC LIMIT ?"
    params.append(limit)
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_result_row(r) for r in rows]


@app.get("/results/{result_id}")
def get_result(result_id: int) -> dict[str, Any]:
    with _conn() as conn:
        row = conn.execute(
            """SELECT r.id AS result_id, r.replaceability_score, r.scoring, r.extraction, r.created_at,
                      l.phone, l.restaurant_name, l.city, l.state,
                      ca.outcome, ca.transcript, ca.duration_seconds
               FROM call_results r
               JOIN leads l ON l.id = r.lead_id
               JOIN call_attempts ca ON ca.id = r.attempt_id
               WHERE r.id = ?""",
            (result_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Result not found")
    body = _result_row(row)
    body["transcript"] = row["transcript"]
    body["duration_seconds"] = row["duration_seconds"]
    return body
