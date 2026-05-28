"""Deterministic mock provider. Hashes the phone for a stable-ish baseline outcome
(same restaurant behaves consistently), but jitters per-attempt so retries differ.

Generates realistic transcripts across a distribution of outcomes so the extractor and
scorer are exercised meaningfully end-to-end."""
from __future__ import annotations

import hashlib
import random
import time
import uuid

from .base import CallOutcome, CallProvider, CallRequest, CallResult

# Outcome distribution. Tuned so a 10-call batch will hit voicemail/no-answer/busy
# at least once, exercising the scheduler's retry path.
_OUTCOME_WEIGHTS: list[tuple[CallOutcome, int]] = [
    (CallOutcome.ANSWERED, 60),
    (CallOutcome.VOICEMAIL, 15),
    (CallOutcome.NO_ANSWER, 15),
    (CallOutcome.BUSY, 7),
    (CallOutcome.FAILED, 3),
]

# Buckets within ANSWERED.
_ANSWERED_FLAVORS = [
    "excellent", "good", "rushed", "language_barrier",
    "no_takeout_today", "online_only", "auto_attendant_lost",
]

_ENTREES = ["the burger", "the pad thai", "a margherita pizza", "the chicken parm",
            "the brisket plate", "the buffalo wings", "the salmon", "the carnitas tacos"]
_SIDES = ["fries", "a Caesar salad", "garlic bread", "coleslaw"]


def _restaurant(req: CallRequest) -> str:
    return req.restaurant_name or "the restaurant"


def _rng_for(phone: str, attempt_salt: str) -> random.Random:
    h = hashlib.sha256(f"{phone}|{attempt_salt}".encode()).digest()
    seed = int.from_bytes(h[:8], "big")
    return random.Random(seed)


def _weighted_choice(rng: random.Random, weights: list[tuple]) -> object:
    total = sum(w for _, w in weights)
    pick = rng.uniform(0, total)
    acc = 0
    for item, w in weights:
        acc += w
        if pick <= acc:
            return item
    return weights[-1][0]


def _transcript_excellent(rng: random.Random, req: CallRequest) -> tuple[str, float]:
    name = _restaurant(req)
    entree = rng.choice(_ENTREES)
    side = rng.choice(_SIDES)
    hold = rng.randint(0, 4)
    dur = 95 + rng.randint(-20, 40)
    lines = [
        f"Host: Thanks for calling {name}, this is Maria, how can I help you?",
        f"Alex: Hi Maria, this is Alex — are you open for takeout right now?",
        f"Host: Yes we are, we're open until 10 PM tonight.",
        f"Alex: Perfect. Can I get {entree}?",
        f"Host: Absolutely, that's one of our most popular. Would you like to add {side} with that? They go really well together.",
        f"Alex: Sure, let's do that. About how long for pickup?",
        f"Host: Should be ready in about 20 minutes.",
        f"Alex: Great. Actually let me call back to confirm — I need to check with my friend first.",
        f"Host: No problem, just ask for Maria when you call back.",
        f"Alex: Thanks so much, talk soon.",
    ]
    return "\n".join(lines), float(dur)


def _transcript_good(rng: random.Random, req: CallRequest) -> tuple[str, float]:
    name = _restaurant(req)
    entree = rng.choice(_ENTREES)
    dur = 75 + rng.randint(-15, 30)
    lines = [
        f"Host: {name}.",
        f"Alex: Hi, are you open for takeout?",
        f"Host: Yeah, til nine.",
        f"Alex: Cool, can I order {entree}?",
        f"Host: Sure. Anything else?",
        f"Alex: Hmm, what would you recommend with it?",
        f"Host: Up to you.",
        f"Alex: Okay just the one. How long for pickup?",
        f"Host: Fifteen minutes.",
        f"Alex: Let me call back to confirm, thanks.",
    ]
    return "\n".join(lines), float(dur)


def _transcript_rushed(rng: random.Random, req: CallRequest) -> tuple[str, float]:
    name = _restaurant(req)
    entree = rng.choice(_ENTREES)
    dur = 50 + rng.randint(-10, 20)
    lines = [
        f"Host: {name}, hold please.",
        f"[hold music for ~45 seconds]",
        f"Host: Sorry about that, what can I get you?",
        f"Alex: Hi, are you open for takeout?",
        f"Host: Yes, what would you like?",
        f"Alex: Can I do {entree}?",
        f"Host: Okay, name?",
        f"Alex: Alex — actually I need to call back, sorry.",
        f"Host: Alright, thanks.",
    ]
    return "\n".join(lines), float(dur)


def _transcript_language_barrier(rng: random.Random, req: CallRequest) -> tuple[str, float]:
    name = _restaurant(req)
    dur = 110 + rng.randint(-20, 30)
    lines = [
        f"Host: Hello? {name}?",
        f"Alex: Hi, are you open for takeout right now?",
        f"Host: Eh… takeout, yes. You order?",
        f"Alex: Yes, what time do you close?",
        f"Host: Close? Nine, ten. Yes.",
        f"Alex: Can I order the burger?",
        f"Host: Burger… yes. One?",
        f"Alex: Yes one. What sides do you have?",
        f"Host: Sorry, one moment, my son help — Hello? Yes, sides are fries, salad.",
        f"Alex: Just the burger then. Let me call back to confirm, thanks.",
    ]
    return "\n".join(lines), float(dur)


def _transcript_no_takeout(rng: random.Random, req: CallRequest) -> tuple[str, float]:
    name = _restaurant(req)
    dur = 35 + rng.randint(-10, 15)
    lines = [
        f"Host: {name}.",
        f"Alex: Hi, are you open for takeout?",
        f"Host: Not tonight, we're dine-in only on weekends.",
        f"Alex: Oh, okay. Any other day?",
        f"Host: Monday through Thursday, yes.",
        f"Alex: Got it, thanks.",
    ]
    return "\n".join(lines), float(dur)


def _transcript_online_only(rng: random.Random, req: CallRequest) -> tuple[str, float]:
    name = _restaurant(req)
    dur = 25 + rng.randint(-5, 10)
    lines = [
        f"Host: {name}, this is Jordan.",
        f"Alex: Hi, can I place a takeout order over the phone?",
        f"Host: We actually only do takeout through our website or DoorDash now.",
        f"Alex: Oh okay, no problem, I'll do that. Thanks.",
    ]
    return "\n".join(lines), float(dur)


def _transcript_auto_attendant(rng: random.Random, req: CallRequest) -> tuple[str, float]:
    name = _restaurant(req)
    dur = 65 + rng.randint(-15, 25)
    lines = [
        f"IVR: Thank you for calling {name}. For hours, press 1. For directions, press 2. To place an order, press 3. To speak with a host, press 0.",
        f"[Alex presses 3]",
        f"IVR: Online ordering is available at our website. To speak with a host, press 0. Or stay on the line.",
        f"[hold music for 60+ seconds]",
        f"[Alex hangs up after waiting]",
    ]
    return "\n".join(lines), float(dur)


_ANSWERED_DISPATCH = {
    "excellent": _transcript_excellent,
    "good": _transcript_good,
    "rushed": _transcript_rushed,
    "language_barrier": _transcript_language_barrier,
    "no_takeout_today": _transcript_no_takeout,
    "online_only": _transcript_online_only,
    "auto_attendant_lost": _transcript_auto_attendant,
}


def _transcript_voicemail(rng: random.Random, req: CallRequest) -> tuple[str, float]:
    name = _restaurant(req)
    dur = 22 + rng.randint(-5, 8)
    lines = [
        f"Voicemail: You've reached {name}. We're not available right now. Our hours are 11 AM to 9 PM Tuesday through Sunday. Please leave a message after the tone.",
        f"Alex: Hi, this is Alex, I was hoping to place a takeout order, I'll try back later, thanks.",
    ]
    return "\n".join(lines), float(dur)


class MockProvider:
    name = "mock"

    def __init__(self, call_delay_seconds: float = 0.0):
        # 0 = return immediately (tests + fast iteration). >0 = sleep with jitter so a live demo
        # feels like real calls instead of an instant flash. A real call is 30-120s; 5-10s is
        # enough for the demo to feel real without being painful.
        self.call_delay_seconds = max(0.0, float(call_delay_seconds))

    def place_call(self, req: CallRequest) -> CallResult:
        # Tiny baseline pause so timing looks plausible in logs even at delay=0.
        time.sleep(0.05)
        if self.call_delay_seconds > 0:
            jitter = random.uniform(0.7, 1.3)
            time.sleep(self.call_delay_seconds * jitter)

        attempt_salt = uuid.uuid4().hex[:6]
        rng = _rng_for(req.phone, attempt_salt)
        outcome = _weighted_choice(rng, _OUTCOME_WEIGHTS)

        call_id = f"mock_{uuid.uuid4().hex[:12]}"
        meta: dict = {"attempt_salt": attempt_salt}

        if outcome == CallOutcome.ANSWERED:
            flavor = rng.choice(_ANSWERED_FLAVORS)
            transcript, dur = _ANSWERED_DISPATCH[flavor](rng, req)
            meta["mock_flavor"] = flavor
            return CallResult(
                outcome=CallOutcome.ANSWERED, provider=self.name,
                provider_call_id=call_id, duration_seconds=dur,
                transcript=transcript, raw_metadata=meta,
            )

        if outcome == CallOutcome.VOICEMAIL:
            transcript, dur = _transcript_voicemail(rng, req)
            return CallResult(
                outcome=CallOutcome.VOICEMAIL, provider=self.name,
                provider_call_id=call_id, duration_seconds=dur,
                transcript=transcript, raw_metadata=meta,
            )

        if outcome == CallOutcome.NO_ANSWER:
            return CallResult(
                outcome=CallOutcome.NO_ANSWER, provider=self.name,
                provider_call_id=call_id, duration_seconds=30.0,
                transcript="[no answer after 30 seconds of ringing]", raw_metadata=meta,
            )

        if outcome == CallOutcome.BUSY:
            return CallResult(
                outcome=CallOutcome.BUSY, provider=self.name,
                provider_call_id=call_id, duration_seconds=2.0,
                transcript="[busy signal]", raw_metadata=meta,
            )

        return CallResult(
            outcome=CallOutcome.FAILED, provider=self.name,
            provider_call_id=call_id, duration_seconds=0.0,
            transcript="[carrier error: call could not be placed]",
            raw_metadata={**meta, "error": "carrier_error"},
        )
