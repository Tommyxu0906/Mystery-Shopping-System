"""SQLite layer. Stdlib only — an ORM is overkill for ~5 tables."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT UNIQUE NOT NULL,
    restaurant_name TEXT,
    website TEXT,
    street_address TEXT,
    raw_address TEXT,
    city TEXT,
    state TEXT,
    country TEXT,
    postal_code TEXT,
    google_reviews_count INTEGER,
    google_maps_url TEXT,
    timezone TEXT,
    status TEXT NOT NULL DEFAULT 'new',  -- new | in_progress | done | excluded
    last_attempt_at TEXT,
    next_eligible_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_next_eligible ON leads(next_eligible_at);

CREATE TABLE IF NOT EXISTS lead_contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    first_name TEXT,
    last_name TEXT,
    email TEXT
);

CREATE INDEX IF NOT EXISTS idx_lead_contacts_lead ON lead_contacts(lead_id);

CREATE TABLE IF NOT EXISTS call_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at TEXT,
    outcome TEXT NOT NULL,  -- answered | voicemail | no_answer | busy | failed
    provider TEXT NOT NULL,
    provider_call_id TEXT,
    duration_seconds REAL,
    transcript TEXT,
    raw_metadata TEXT  -- JSON
);

CREATE INDEX IF NOT EXISTS idx_call_attempts_lead ON call_attempts(lead_id);
CREATE INDEX IF NOT EXISTS idx_call_attempts_outcome ON call_attempts(outcome);

CREATE TABLE IF NOT EXISTS call_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER NOT NULL UNIQUE REFERENCES call_attempts(id) ON DELETE CASCADE,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    extraction TEXT NOT NULL,  -- JSON from extractor
    scoring TEXT NOT NULL,     -- JSON from scorer
    overall_score REAL NOT NULL,
    replaceability_score REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_call_results_lead ON call_results(lead_id);
CREATE INDEX IF NOT EXISTS idx_call_results_replaceability ON call_results(replaceability_score DESC);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)  # autocommit; we manage transactions explicitly
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, separators=(",", ":"))
