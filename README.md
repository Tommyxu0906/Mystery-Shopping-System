# Maple Mystery Shop

A working pipeline that mystery-shops restaurant leads — calls them as a customer
trying to place a takeout order, extracts structured signal from the transcript, and
scores each call so an SDR can act on it in five seconds.

```
xlsx ─▶ ingest ─▶ leads (SQLite)
                    │
                    ▼
              scheduler (tz, business hours, retry/cooldown)
                    │
                    ▼
          CallProvider [mock | vapi]      ◀── agent_script (persona + system prompt)
                    │
                    ▼
              call_attempts (transcript, outcome, duration)
                    │
                    ▼
             Extractor [llm | heuristic]  ── descriptive JSON
                    │
                    ▼
                  Scorer                  ── fit · pain · replaceability · tier · one-liner
                    │
                    ▼
              call_results (queryable)    ──▶ CLI / FastAPI
```

---

## Quick start

```bash
# Python 3.11+ required (uses stdlib zoneinfo, PEP 604 unions)
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env       # works as-is; mock provider, heuristic extractor

mystery-shop ingest data/leads.xlsx
mystery-shop run --batch 15
mystery-shop results --limit 10
mystery-shop stats

# Optional: read API
uvicorn mystery_shop.api:app --reload
#   GET  http://localhost:8000/results?tier=hot
#   GET  http://localhost:8000/results/1
#   GET  http://localhost:8000/stats

# Tests
pytest -q
```

Zero credentials needed for the default path. Set `OPENAI_API_KEY` to swap the
heuristic extractor for `gpt-4o-mini` structured-output extraction. Set
`CALL_PROVIDER=vapi` plus `VAPI_*` to place real calls.

---

## Live demo (for the 30-min interview session)

Three commands that show the system working end-to-end in under 60 seconds:

```bash
# 1. Place a small batch and see the outcome distribution + tier breakdown
mystery-shop -v run --batch 5
#  -> {"calls_placed": 5, "outcomes": {...}, "tiers": {"hot": 2, ...}, "errors": 0}

# 2. Pull the top hot lead with the full SDR briefing
mystery-shop results --limit 1

# 3. Or hit the API for the SDR-facing view
uvicorn mystery_shop.api:app --reload  # then open http://localhost:8000/results?tier=hot
```

To prove the error-handling story (no-op without it, but worth showing on a code walk):

```bash
# The orchestrator already survives provider exceptions — see
# tests/test_pipeline.py::test_run_batch_survives_a_provider_exception
pytest -q -k provider_exception
```

---

## Sample output (one row, real run)

This is a real `mystery-shop results --limit 1` row from a fresh batch — copy of the
structure you'll see in [`samples/sample_results.json`](samples/sample_results.json):

```json
{
  "result_id": 17,
  "restaurant": "Thekniferestaurant",
  "phone": "+17862320040",
  "location": "Miami, Florida",
  "outcome": "no_answer",
  "duration_seconds": 30.0,
  "extraction": {
    "answered_by_human": false,
    "voicemail": false,
    "ivr_present": false,
    "hold_time_seconds": null,
    "stated_close_time": null,
    "stated_open_for_takeout": null,
    "order_accepted": false,
    "order_refused_reason": null,
    "upsell_attempted": false,
    "estimated_pickup_minutes": null,
    "host_name": null,
    "english_fluency": "unknown",
    "friendliness": "unknown",
    "hung_up_by": "system",
    "notable_quotes": [],
    "summary_one_line": "No answer after ringing.",
    "extractor": "heuristic"
  },
  "scoring": {
    "fit_score": 90.0,
    "pain_score": 95.0,
    "replaceability_score": 85.5,
    "answer_quality": 5.0,
    "order_handling": 0.0,
    "host_quality": 0.0,
    "urgency_tier": "hot",
    "sdr_one_liner": "[HOT] No one answered during business hours — they're losing orders right now.",
    "reasons": ["Phone rang out — no one picked up."]
  }
}
```

The `sdr_one_liner` and `urgency_tier` are what surfaces in the CRM row. Everything
else is for "click to expand."

---

## What's mocked, what's real

| Layer | Default | Real path |
| --- | --- | --- |
| Lead ingest | Real — full 2,355-row xlsx parsed and deduped | — |
| SQLite schema, scheduler, retry | Real | — |
| Agent script (persona + system prompt) | Real | — |
| **Call placement** | **Mock** — deterministic per-phone hash, 5-outcome distribution, 7 answered flavors | `VapiProvider` in [`providers/vapi.py`](src/mystery_shop/providers/vapi.py) hits real Vapi `/call` API |
| **Transcript** | **Mock** — templated dialogue per flavor with restaurant name + menu interpolation | Real, from Vapi |
| **Extraction** | Heuristic regex (deterministic, lossless on mock templates) | `LLMExtractor` uses OpenAI structured outputs — kicks in if `OPENAI_API_KEY` is set. Falls back to heuristic automatically on any OpenAI failure (network, rate-limit, schema drift) and stamps the result with `extractor: "heuristic_llm_fallback"`. |
| Scoring | Real — same code regardless of upstream | — |
| FastAPI read endpoints | Real | — |

The mock provider is **not** a stub — it returns realistic varied transcripts so
the extractor, scorer, and scheduler are exercised end-to-end. You can sort the
mock leaderboard by replaceability and the answers make sense.

---

## Scoring rubric (the part you're evaluating most)

Maple sells AI phone answering to restaurants. So the question isn't "is this a
good restaurant?" — it's "would Maple's product clearly help this restaurant,
and would an SDR's outreach land?"

I compute three numbers an SDR can act on:

1. **`fit_score` (0–100)** — does Maple even apply? An online-only place has
   `fit_score=5` regardless of how chaotic their phone is, because Maple replaces
   the phone-answering job, and there is no job to replace.

2. **`pain_score` (0–100)** — how bad is the current experience right now?
   Voicemail / no-answer / busy / IVR-with-no-human / long holds / refused orders
   raise this. Warm host taking an order with an upsell in 90 seconds lowers it.

3. **`replaceability_score = fit × pain / 100`** — the headline. Sorts the queue.
   High = call them first.

Plus three sub-scores that make the headline auditable when an SDR asks "why is
this a 76?":

- `answer_quality` — pickup + hold time + IVR friction
- `order_handling` — order_accepted + pickup ETA + upsell behavior
- `host_quality` — friendliness + English fluency + hours accuracy

And a one-liner per call so the SDR doesn't need to read JSON:

> `[HOT] Phone goes to voicemail mid-day; clear case for AI answering.`
> `[HOT] Line busy during business hours — likely understaffed phones.`
> `[SKIP] Online-only ordering. Maple's phone product doesn't fit.`
> `[COLD] Strong human host: warm, on-script, upsells. Hard sell unless they want to scale.`

### Why this shape

- **Extraction is separated from scoring.** The extractor outputs descriptive
  facts ("upsell_attempted: false", "english_fluency: limited") with no
  opinions. The scorer is the only place opinions about Maple's value prop live.
  Means we can re-weight the rubric without re-running calls.
- **Pain is bounded by fit.** A loud, broken phone at an online-only spot still
  scores low — we shouldn't waste an SDR's morning on it.
- **Non-answered outcomes get high pain by definition.** A no-answer during
  business hours is literally a lost order; it's the cleanest possible Maple
  pitch. The rubric reflects that.

---

## Call orchestration

- **Per-state → IANA timezone map** in [`timezones.py`](src/mystery_shop/timezones.py).
  Split states (FL, KY, IN, MI, TN, TX, etc.) collapse to the dominant tz. For a
  real system you'd geocode the postal code or use a TZ shapefile — flagged
  in-code.
- **Business hours: 11am–8pm local** by default. Configurable in
  [`config.py`](src/mystery_shop/config.py).
- **Retry policy** ([`scheduler.py`](src/mystery_shop/scheduler.py)):
  - `answered` / `voicemail` → done
  - `no_answer` → cool down 2h, max 3 attempts
  - `busy` → cool down 30m, max 3 attempts
  - `failed` (carrier error) → cool down 24h, retry once
- **Dedup by phone at ingest.** The sheet has multiple owners per restaurant
  (Jimmy Hula's appears with three). One `leads` row per phone; every owner is
  preserved in `lead_contacts` for SDR follow-up.
- **`claim_next_batch` is atomic** — selects, filters by business hours in
  Python (the DB doesn't know about timezones), marks `in_progress`, all inside
  one transaction. Safe under a future worker pool.
- **Skip-reason logging.** When the scheduler returns fewer leads than
  requested, it logs the breakdown — `outside_business_hours`, `no_timezone`,
  `cooling_down`, `maxed_attempts`. Operators can tell "the queue is empty"
  apart from "every callable lead is on the east coast and it's 2am there."

---

## Error handling & recovery

The pipeline is designed to **never leave a lead stuck**.

- **Per-lead try/except in the orchestrator.** If anything throws while
  processing a lead — provider timeout, OpenAI 503, DB hiccup — we catch it
  at the batch boundary, log it, and write a synthetic `failed` `call_attempt`
  row capturing the exception. The lead is then run through the standard retry
  policy (24h cooldown, retry once), so a transient outage degrades gracefully
  instead of stalling the queue.
- **LLM extractor falls back to heuristic** on any OpenAI error (network,
  rate-limit, auth, schema drift, or invalid JSON). The fallback Extraction is
  stamped with `extractor: "heuristic_llm_fallback"` so the UI and SDRs know
  what happened. Cost guardrail too: we never pay for an LLM call on a
  non-answered outcome or on an empty transcript.
- **Empty / truncated transcripts** (Vapi can lose audio capture even on a
  successful answered call) are detected and produce an honest degraded record
  with `answered_by_human: true` but `summary_one_line` flagging the capture
  failure — better than letting the regex hallucinate fields.
- **Connection lifecycle is explicit.** `api.py` uses FastAPI `Depends(get_db)`
  to open + close per request. `cli.py` uses `contextlib.closing` so command
  exits don't leak file handles.

Covered by tests:
`test_run_batch_survives_a_provider_exception`,
`test_llm_extractor_falls_back_to_heuristic_on_api_error`,
`test_heuristic_handles_empty_transcript_on_answered`.

---

## Code layout

```
src/mystery_shop/
  config.py          env-var loader, single source of truth
  db.py              sqlite3 + schema + transaction helper
  timezones.py       state → IANA tz
  ingest.py          xlsx → leads + lead_contacts (dedup, COALESCE updates)
  agent_script.py    customer persona + system prompt (Vapi-ready)
  providers/
    base.py          CallProvider Protocol + CallRequest/Result/Outcome
    mock.py          deterministic mock, 7 answered flavors + 4 other outcomes
    vapi.py          real-API-shape adapter (raises clearly if creds missing)
    factory.py       resolves provider from config
  extractor.py       Extraction dataclass; HeuristicExtractor + LLMExtractor
  scorer.py          rubric, sub-scores, tier, sdr_one_liner
  scheduler.py       business hours, claim_next_batch, retry policy
  orchestrator.py    end-to-end pipeline; thin glue
  cli.py             argparse: init / ingest / run / results / stats
  api.py             FastAPI read endpoints
tests/
  test_pipeline.py   ingest, extractor, scorer, scheduler, e2e
```

Every module is one concern. The orchestrator never imports openpyxl, openai, or
httpx — those are sealed behind their owning module.

---

## Data extracted per call

```jsonc
{
  "answered_by_human":      true,
  "voicemail":              false,
  "ivr_present":            false,
  "hold_time_seconds":      0,
  "stated_close_time":      "10 PM",
  "stated_open_for_takeout":true,
  "order_accepted":         true,
  "order_refused_reason":   null,           // or "online_only" | "dine_in_only" | "closed" | "no_takeout" | "other"
  "upsell_attempted":       true,
  "estimated_pickup_minutes": 20,
  "host_name":              "Maria",
  "english_fluency":        "fluent",       // fluent | partial | limited | unknown
  "friendliness":           "warm",         // warm | neutral | curt | rude | unknown
  "hung_up_by":             "customer",     // customer | host | system | unknown
  "notable_quotes":         ["..."],
  "summary_one_line":       "...",
  "extractor":              "heuristic"     // or "llm"
}
```

Scoring object is in [`scorer.py`](src/mystery_shop/scorer.py); sample output is
in [`samples/sample_results.json`](samples/sample_results.json).

---

## Cost estimate

Per call, against live providers:

| Item | Approx cost |
| --- | --- |
| Vapi voice call (90s avg) | $0.05–0.15 per minute → ~$0.10–0.20/call |
| GPT-4o-mini extraction (~1500 input + 300 output tokens) | ~$0.0005/call |
| GPT-4o extraction (if you want higher fidelity) | ~$0.01/call |
| Storage | Negligible |

For the full 2,058 unique numbers: **~$200–400 in voice + ~$1 in LLM.** The
extractor is essentially free compared to the call itself, so don't optimize
that side until the call cost stops dominating.

---

## What I'd build next (one more week)

1. **Real Vapi run on a 50-call slice** to validate the script in the wild and
   tune `endCallFunctionEnabled` / hangup heuristics against real audio.
2. **An LLM-based extractor evaluation suite** — 30 hand-labeled transcripts;
   precision/recall per field. Right now I'm trusting LLM JSON-mode without a
   regression gate.
3. **Smarter scheduling** — rate-limit by area code (don't blast a city), random
   jitter inside the call window, lunch/dinner-rush awareness.
4. **Geocoded timezones** — fix the split-state simplification with a postal-code
   lookup.
5. **Owner-side data on the result** — when an SDR pulls a hot lead, surface the
   linked `lead_contacts` (names + email) on the same view.
6. **Webhook callback flow for Vapi** — current `VapiProvider` polls; webhook is
   the right shape for any nontrivial volume.
7. **Cost guardrails** — daily call budget, kill-switch, per-tz queue caps.

---

## Trade-offs and what I deliberately didn't do

- **No Postgres / no Alembic.** SQLite + raw SQL is perfect for take-home scale.
  Schema in [`db.py`](src/mystery_shop/db.py); migrating to Postgres is a
  ~30-minute job because we use SQL that parses on both.
- **No async / no worker pool.** `run_batch` is sequential. With a real provider
  it's IO-bound on the poll loop — async or a thread pool would be the next move.
  Not needed for the take-home and would obscure the design.
- **No ORM.** Five tables, hand-written SQL is clearer than wrestling with
  SQLAlchemy types here.
- **Heuristic extractor is tailored to mock transcripts.** That's deliberate: it
  gives the project a zero-config working demo and a deterministic baseline. The
  LLM extractor is the production path.
- **No retry on the LLM extraction call** — a transient failure should just
  re-enqueue the lead through the normal scheduler; no need for a second retry
  layer inside extraction.
- **No auth on the FastAPI read endpoints.** Internal SDR tool — would gate
  behind your existing org auth in production.

---

## License / attribution

Built for Maple's take-home interview round 2.
Generated lead data from the provided Google Sheet.
