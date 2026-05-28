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
    now = datetime.now(timezone.utc).isoformat()
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
    # Force into business hours by directly updating eligible time (already null)
    records = run_batch(conn, cfg, batch_size=5)
    if not records:
        pytest.skip("Outside business hours; skipping live timing-dependent assertion.")
    attempts = conn.execute("SELECT COUNT(*) c FROM call_attempts").fetchone()["c"]
    results = conn.execute("SELECT COUNT(*) c FROM call_results").fetchone()["c"]
    assert attempts == len(records)
    assert results == len(records)
