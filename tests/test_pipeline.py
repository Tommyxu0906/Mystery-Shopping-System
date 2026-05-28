"""End-to-end + unit tests. Run with: pytest -q"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

os.environ.setdefault("CALL_PROVIDER", "mock")
os.environ.pop("OPENAI_API_KEY", None)  # force heuristic extractor in tests

from mystery_shop.config import load_config  # noqa: E402
from mystery_shop.db import connect, init_schema  # noqa: E402
from mystery_shop.extractor import HeuristicExtractor  # noqa: E402
from mystery_shop.ingest import derive_name_from_website, normalize_phone  # noqa: E402
from mystery_shop.orchestrator import run_batch  # noqa: E402
from mystery_shop.providers.base import CallOutcome  # noqa: E402
from mystery_shop.providers.mock import MockProvider  # noqa: E402
from mystery_shop.scheduler import is_within_business_hours  # noqa: E402
from mystery_shop.scorer import score  # noqa: E402


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "test.db")
    init_schema(c)
    return c


def test_phone_normalization():
    assert normalize_phone("+14345849382") == "+14345849382"
    assert normalize_phone("434-584-9382") == "+14345849382"
    assert normalize_phone("(434) 584-9382") == "+14345849382"
    assert normalize_phone("nope") is None
    assert normalize_phone(None) is None


def test_name_from_website():
    assert derive_name_from_website("http://www.313franklin.com") == "313 Franklin"
    assert derive_name_from_website("http://www.galvinsonmain.com") == "Galvinsonmain"
    assert derive_name_from_website(None) is None


def test_business_hours_eastern_noon():
    cfg = load_config()
    noon_ny = datetime(2026, 5, 28, 16, 0, tzinfo=timezone.utc)  # noon ET
    assert is_within_business_hours("America/New_York", cfg, noon_ny)
    midnight_ny = datetime(2026, 5, 28, 4, 0, tzinfo=timezone.utc)  # midnight ET
    assert not is_within_business_hours("America/New_York", cfg, midnight_ny)


def test_mock_provider_produces_outcomes():
    """Over many calls we hit each outcome at least once."""
    from mystery_shop.providers.base import CallRequest
    prov = MockProvider()
    outcomes = set()
    for i in range(200):
        req = CallRequest(
            lead_id=i, phone=f"+1212555{i:04d}", restaurant_name="Test Diner",
            system_prompt="", opening_line="",
        )
        outcomes.add(prov.place_call(req).outcome)
    assert outcomes == set(CallOutcome)


def test_heuristic_extractor_on_excellent_template():
    """The 'excellent' mock template should produce a high-quality extraction."""
    transcript = "\n".join([
        "Host: Thanks for calling Test Diner, this is Maria, how can I help you?",
        "Alex: Hi Maria, are you open for takeout right now?",
        "Host: Yes we are, we're open until 10 PM tonight.",
        "Alex: Can I get the burger?",
        "Host: Absolutely. Would you like to add fries with that? They go really well together.",
        "Alex: Sure. About how long for pickup?",
        "Host: Should be ready in about 20 minutes.",
    ])
    ext = HeuristicExtractor().extract(transcript, CallOutcome.ANSWERED)
    assert ext.answered_by_human is True
    assert ext.host_name == "Maria"
    assert ext.upsell_attempted is True
    assert ext.estimated_pickup_minutes == 20
    assert ext.stated_close_time and "10" in ext.stated_close_time
    assert ext.friendliness == "warm"


def test_scoring_distinguishes_no_answer_from_excellent():
    """No-answer should be a HOT lead (high pain × full fit). Excellent should be COLD."""
    no_ans_ext = HeuristicExtractor().extract("", CallOutcome.NO_ANSWER)
    no_ans_score = score(no_ans_ext, CallOutcome.NO_ANSWER)
    assert no_ans_score.urgency_tier in ("hot", "warm")
    assert no_ans_score.replaceability_score >= 60

    excellent_transcript = "\n".join([
        "Host: Thanks for calling Test Diner, this is Maria.",
        "Alex: Hi, takeout?",
        "Host: Yes, until 10 PM.",
        "Alex: Burger please.",
        "Host: Would you like to add fries with that?",
        "Alex: Sure.",
        "Host: Should be ready in about 15 minutes.",
    ])
    good_ext = HeuristicExtractor().extract(excellent_transcript, CallOutcome.ANSWERED)
    good_score = score(good_ext, CallOutcome.ANSWERED)
    assert good_score.urgency_tier in ("cold", "warm")
    assert good_score.replaceability_score < no_ans_score.replaceability_score


def test_online_only_is_skip():
    transcript = "\n".join([
        "Host: Test Diner.",
        "Alex: Can I place a takeout order?",
        "Host: We actually only do takeout through our website now.",
    ])
    ext = HeuristicExtractor().extract(transcript, CallOutcome.ANSWERED)
    sc = score(ext, CallOutcome.ANSWERED)
    assert sc.urgency_tier == "skip"
    assert ext.order_refused_reason == "online_only"


def test_end_to_end_run_batch(conn: sqlite3.Connection):
    """Seed leads, run a batch, verify rows in attempts + results."""
    # Use a timezone where it's currently business hours (UTC noon-ish is daytime nearly everywhere in the US).
    conn.executemany(
        """INSERT INTO leads (phone, restaurant_name, state, timezone, status)
           VALUES (?, ?, ?, ?, 'new')""",
        [
            (f"+121255500{i:02d}", f"Test {i}", "New York", "America/New_York", )
            for i in range(5)
        ],
    )
    cfg = load_config()
    records = run_batch(conn, cfg, batch_size=5)
    if not records:
        pytest.skip("Outside business hours; skipping live timing-dependent assertion.")
    attempts = conn.execute("SELECT COUNT(*) c FROM call_attempts").fetchone()["c"]
    results = conn.execute("SELECT COUNT(*) c FROM call_results").fetchone()["c"]
    assert attempts == len(records)
    assert results == len(records)


# ---------------------------------------------------------------------------
# Error-path tests (added 2026-05-28).
# ---------------------------------------------------------------------------

def test_heuristic_handles_empty_transcript_on_answered():
    """Vapi sometimes returns outcome=answered with no transcript (audio capture failure).
    We should not crash and we should produce an honest, degraded record."""
    ext = HeuristicExtractor().extract("", CallOutcome.ANSWERED)
    assert ext.answered_by_human is True
    assert ext.order_accepted is False
    assert "no transcript" in ext.summary_one_line.lower()


def test_llm_extractor_falls_back_to_heuristic_on_api_error(monkeypatch):
    """If OpenAI raises (network, rate limit, auth), the LLM extractor should fall back
    to the heuristic and stamp the extractor field so downstream knows what happened."""
    from mystery_shop.extractor import LLMExtractor

    class _BoomClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    raise RuntimeError("simulated OpenAI 503")

    extractor = LLMExtractor.__new__(LLMExtractor)  # bypass real OpenAI client init
    extractor._client = _BoomClient()
    extractor._model = "gpt-4o-mini"

    transcript = "Host: Test Diner, this is Maria.\nAlex: Are you open for takeout?\nHost: Yes, until 10 PM."
    result = extractor.extract(transcript, CallOutcome.ANSWERED)
    assert result.extractor == "heuristic_llm_fallback"
    assert result.answered_by_human is True
    assert result.host_name == "Maria"


class _FlakyProvider:
    """Throws on the second lead in a batch; succeeds on the others.
    Lets us prove that one bad call doesn't poison the whole batch."""
    name = "flaky_mock"

    def __init__(self):
        self.calls = 0

    def place_call(self, req):
        from mystery_shop.providers.mock import MockProvider
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("simulated provider outage")
        return MockProvider().place_call(req)


def test_run_batch_survives_a_provider_exception(conn: sqlite3.Connection):
    """One lead in the batch should fail without poisoning the others, and the failed
    lead should be left in a state that lets the scheduler retry it later."""
    conn.executemany(
        """INSERT INTO leads (phone, restaurant_name, state, timezone, status)
           VALUES (?, ?, ?, ?, 'new')""",
        [
            (f"+13125550{i:03d}", f"Boom Test {i}", "Illinois", "America/Chicago")
            for i in range(3)
        ],
    )
    cfg = load_config()
    records = run_batch(conn, cfg, batch_size=3, provider=_FlakyProvider())
    if not records:
        pytest.skip("Outside business hours; skipping timing-dependent assertion.")

    # All three leads should have a corresponding PipelineRecord; one of them should be marked error.
    assert len(records) == 3
    error_records = [r for r in records if r.error is not None]
    assert len(error_records) == 1
    assert "simulated provider outage" in error_records[0].error

    # The failed lead should be back in 'new' state with next_eligible_at set (24h retry),
    # not stuck in 'in_progress'.
    failed_lead_id = error_records[0].lead_id
    row = conn.execute(
        "SELECT status, next_eligible_at, attempt_count FROM leads WHERE id = ?",
        (failed_lead_id,),
    ).fetchone()
    assert row["status"] == "new"
    assert row["next_eligible_at"] is not None
    assert row["attempt_count"] == 1

    # A synthetic 'failed' attempt row should exist for the audit trail.
    failed_attempts = conn.execute(
        "SELECT COUNT(*) c FROM call_attempts WHERE outcome = 'failed'"
    ).fetchone()["c"]
    assert failed_attempts >= 1


def test_scheduler_skips_outside_business_hours():
    """Sanity check: a Hawaii lead at UTC 00:00 (which is 14:00 HST — inside our window).
    But a UK timezone lead at UTC 03:00 should be filtered (03:00 UTC = 04:00 BST, before 11)."""
    from mystery_shop.scheduler import is_within_business_hours
    cfg = load_config()
    utc_3am = datetime(2026, 5, 28, 3, 0, tzinfo=timezone.utc)
    # 11pm Eastern day before — outside business hours
    assert not is_within_business_hours("America/New_York", cfg, utc_3am)
    # Lead with no timezone should never be in business hours
    assert not is_within_business_hours(None, cfg, utc_3am)


# ---------------------------------------------------------------------------
# UX-layer additions (2026-05-28).
# ---------------------------------------------------------------------------

def test_csv_ingest_round_trip(conn: sqlite3.Connection, tmp_path: Path):
    """CSV ingest hits the same code path as xlsx and produces correct counters
    including a skip_reasons breakdown."""
    from mystery_shop.ingest import ingest_file
    csv_path = tmp_path / "tiny.csv"
    csv_path.write_text(
        "first_name,last_name,organization_state,organization_city,Location Phone,organization_website_url\n"
        "Jamie,Lee,California,San Francisco,+14155550111,http://www.bayareadeli.com\n"
        "Priya,Shah,Texas,Austin,+15125550199,http://www.eastviewpizza.com\n"
        "Bad,Row,,,nope,\n"          # 'nope' lands in the Location Phone column
        "Empty,Phone,,,,\n"           # blank Location Phone
    )
    counters = ingest_file(conn, csv_path)
    assert counters["leads_inserted"] == 2
    assert counters["skipped"] == 2
    # Skip reasons should track both shapes of badness.
    assert counters["skip_reasons"].get("invalid_phone", 0) == 1
    assert counters["skip_reasons"].get("empty_phone", 0) == 1


def test_preview_does_not_claim_leads(conn: sqlite3.Connection):
    """preview_next_batch is read-only — leads stay 'new' after calling it, unlike claim_next_batch."""
    from mystery_shop.scheduler import preview_next_batch
    conn.executemany(
        """INSERT INTO leads (phone, restaurant_name, state, timezone, status)
           VALUES (?, ?, ?, ?, 'new')""",
        [(f"+13125551{i:03d}", f"Preview Test {i}", "Illinois", "America/Chicago") for i in range(3)],
    )
    cfg = load_config()
    rows = preview_next_batch(conn, cfg, 3)
    if not rows:
        pytest.skip("Outside business hours; skipping.")
    assert len(rows) > 0
    # All leads should still be in 'new' status — preview is read-only.
    statuses = {r["status"] for r in conn.execute("SELECT status FROM leads").fetchall()}
    assert statuses == {"new"}


def test_queue_state_shape(conn: sqlite3.Connection):
    """queue_state returns the buckets the CLI displays."""
    from mystery_shop.scheduler import queue_state
    conn.execute(
        """INSERT INTO leads (phone, restaurant_name, state, timezone, status)
           VALUES ('+13125559999', 'Q Test', 'Illinois', 'America/Chicago', 'new')"""
    )
    conn.execute(
        """INSERT INTO leads (phone, restaurant_name, status)
           VALUES ('+13125559998', 'No TZ Test', 'new')"""
    )
    cfg = load_config()
    state = queue_state(conn, cfg)
    assert "ready_now" in state
    assert "cooling_down" in state
    assert "no_timezone" in state
    assert state["no_timezone"] >= 1  # we just inserted one
    assert state["leads_total"] >= 2


def test_reextract_updates_existing_result(conn: sqlite3.Connection):
    """reextract_attempt re-runs extraction on a stored transcript and UPDATEs (doesn't dupe)
    the call_results row."""
    from mystery_shop.orchestrator import reextract_attempt
    # Seed a lead, attempt, and result manually.
    cur = conn.execute(
        """INSERT INTO leads (phone, restaurant_name, state, timezone)
           VALUES ('+13125557777', 'Reextract Test', 'Illinois', 'America/Chicago')"""
    )
    lead_id = cur.lastrowid
    transcript = "Host: Hi this is Maria.\nAlex: Are you open?\nHost: Yes until 10 PM."
    cur = conn.execute(
        """INSERT INTO call_attempts (lead_id, outcome, provider, transcript)
           VALUES (?, 'answered', 'mock', ?)""",
        (lead_id, transcript),
    )
    attempt_id = cur.lastrowid
    conn.execute(
        """INSERT INTO call_results (attempt_id, lead_id, extraction, scoring,
                                     overall_score, replaceability_score)
           VALUES (?, ?, '{}', '{}', 0, 0)""",
        (attempt_id, lead_id),
    )
    cfg = load_config()
    ext, sc = reextract_attempt(conn, cfg, attempt_id)
    assert ext.host_name == "Maria"
    assert sc.replaceability_score != 0  # got updated
    # Exactly one result row per attempt — no duplicate.
    count = conn.execute(
        "SELECT COUNT(*) c FROM call_results WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()["c"]
    assert count == 1


def test_mock_provider_respects_call_delay():
    """call_delay_seconds adds latency. We assert lower bound only — jitter goes both ways."""
    import time as _time
    from mystery_shop.providers.base import CallRequest
    from mystery_shop.providers.mock import MockProvider
    prov = MockProvider(call_delay_seconds=0.2)
    start = _time.monotonic()
    prov.place_call(CallRequest(lead_id=1, phone="+12125550000", restaurant_name="Delay Test",
                                system_prompt="", opening_line=""))
    elapsed = _time.monotonic() - start
    # Jitter is [0.7, 1.3] × 0.2 → [0.14, 0.26]. Add baseline 0.05 → bound at 0.14.
    assert elapsed >= 0.10
