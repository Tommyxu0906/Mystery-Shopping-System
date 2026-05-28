"""Scoring rubric — turns descriptive Extraction into a sales-actionable Score.

DESIGN RATIONALE (also covered in README):

Maple sells an AI phone answering service to restaurants. So the scoring question
isn't "is this a good restaurant?" — it's "would Maple's product clearly help this
restaurant, and is the SDR's outreach likely to land?"

We compute three things an SDR can use in five seconds:

  1. fit_score (0-100):
     Does Maple even solve a problem here? An online-only place has fit=0
     regardless of how chaotic their phone is, because Maple's AI replaces the
     phone-answering job, and there's no job to replace.

  2. pain_score (0-100):
     How bad is their CURRENT phone experience? Voicemail, no-answer, long holds,
     IVR-with-no-human, refused orders all add pain. Warm host taking an order
     with an upsell in 90 seconds = low pain.

  3. replaceability_score = fit_score * pain_score / 100
     The headline number. High = strong lead. Sorts the queue.

We also bucket each call into an urgency_tier (hot/warm/cold/skip) and write a one-line
SDR briefing — these are what shows up in the Maple-internal CRM view.

Sub-scores (answer_quality, order_handling, host_quality) make the headline auditable —
when an SDR asks "why is this a 78?", we can show the breakdown."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

from .extractor import Extraction
from .providers.base import CallOutcome


UrgencyTier = Literal["hot", "warm", "cold", "skip"]


@dataclass
class Score:
    fit_score: float
    pain_score: float
    replaceability_score: float
    answer_quality: float
    order_handling: float
    host_quality: float
    urgency_tier: UrgencyTier
    sdr_one_liner: str
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _clip(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _answer_quality(ext: Extraction, outcome: CallOutcome) -> tuple[float, list[str]]:
    """How well did the restaurant answer the phone? 100 = warm human, no hold."""
    reasons: list[str] = []
    if outcome == CallOutcome.NO_ANSWER:
        reasons.append("Phone rang out — no one picked up.")
        return 5.0, reasons
    if outcome == CallOutcome.BUSY:
        reasons.append("Line was busy.")
        return 10.0, reasons
    if outcome == CallOutcome.FAILED:
        reasons.append("Call could not be placed (carrier error).")
        return 0.0, reasons
    if outcome == CallOutcome.VOICEMAIL:
        reasons.append("Routes to voicemail during stated business hours.")
        return 15.0, reasons

    # ANSWERED
    score = 100.0
    if ext.ivr_present:
        score -= 30
        reasons.append("IVR phone tree before reaching a human.")
    hold = ext.hold_time_seconds or 0
    if hold > 0:
        penalty = min(40, hold * 0.7)
        score -= penalty
        reasons.append(f"~{hold}s hold before being helped.")
    if ext.hung_up_by == "customer" and not ext.order_accepted and ext.ivr_present:
        score -= 10
        reasons.append("IVR never routed to a human; caller abandoned.")
    return _clip(score), reasons


def _order_handling(ext: Extraction, outcome: CallOutcome) -> tuple[float, list[str]]:
    """Could they actually take the order, and did they sell well while doing it?"""
    if outcome != CallOutcome.ANSWERED:
        return 0.0, []
    reasons: list[str] = []
    score = 50.0
    if ext.order_accepted:
        score += 25
        reasons.append("Took the order successfully.")
    else:
        score -= 25
        if ext.order_refused_reason == "online_only":
            reasons.append("Doesn't take phone orders — online-only.")
        elif ext.order_refused_reason == "dine_in_only":
            reasons.append("Dine-in only on this day.")
        elif ext.order_refused_reason:
            reasons.append(f"Order refused: {ext.order_refused_reason}.")
        else:
            reasons.append("Order not completed.")
    if ext.upsell_attempted:
        score += 15
        reasons.append("Attempted an upsell.")
    elif ext.order_accepted:
        score -= 10
        reasons.append("No upsell attempt — leaving money on the table.")
    if ext.estimated_pickup_minutes is not None:
        score += 5
        reasons.append(f"Gave a pickup ETA (~{ext.estimated_pickup_minutes} min).")
    return _clip(score), reasons


def _host_quality(ext: Extraction, outcome: CallOutcome) -> tuple[float, list[str]]:
    """How was the person on the other end as a brand representative?"""
    if outcome != CallOutcome.ANSWERED:
        return 0.0, []
    reasons: list[str] = []
    base = {"warm": 90, "neutral": 65, "curt": 35, "rude": 10, "unknown": 50}[ext.friendliness]
    fluency_adj = {"fluent": 0, "partial": -10, "limited": -25, "unknown": -5}[ext.english_fluency]
    score = base + fluency_adj
    if ext.friendliness == "warm":
        reasons.append("Warm, welcoming tone.")
    if ext.friendliness in ("curt", "rude"):
        reasons.append(f"Host came across {ext.friendliness}.")
    if ext.english_fluency in ("partial", "limited"):
        reasons.append("Language barrier may be costing them orders.")
    if ext.stated_close_time:
        score += 5
        reasons.append("Quoted closing time clearly.")
    return _clip(score), reasons


def _fit_score(ext: Extraction, outcome: CallOutcome) -> tuple[float, list[str]]:
    """Does Maple's product even apply here?"""
    reasons: list[str] = []
    if outcome == CallOutcome.FAILED:
        # Bad data — number may not be valid. Skip.
        reasons.append("Bad/unreachable number — likely a data quality issue.")
        return 0.0, reasons
    if ext.order_refused_reason == "online_only":
        reasons.append("Online-only — Maple has no phone job to replace here.")
        return 5.0, reasons
    if outcome == CallOutcome.ANSWERED and ext.order_refused_reason == "dine_in_only":
        reasons.append("No takeout business on this day — partial fit only.")
        return 40.0, reasons
    # Default: phones matter for this restaurant.
    return 90.0, reasons


def _pain_score(answer_q: float, order_h: float, host_q: float, outcome: CallOutcome) -> float:
    """Higher = worse current experience = stronger Maple value prop.

    For non-answered outcomes (voicemail, no-answer, busy), the pain is enormous:
    a calling customer almost certainly went somewhere else. So those score high
    on pain even though the sub-scores aren't all populated."""
    if outcome == CallOutcome.NO_ANSWER:
        return 95.0  # huge pain — actively losing orders right now
    if outcome == CallOutcome.BUSY:
        return 85.0  # same root cause, slightly less stark
    if outcome == CallOutcome.VOICEMAIL:
        return 80.0  # losing orders during business hours
    if outcome == CallOutcome.FAILED:
        return 0.0   # can't infer pain from a failed call

    # Answered — invert the call quality to get pain. Weight by what matters most
    # to a restaurant's bottom line: did the order land, then how the call felt.
    quality_blend = 0.5 * order_h + 0.3 * answer_q + 0.2 * host_q
    return _clip(100.0 - quality_blend)


def _tier(replaceability: float, fit: float, outcome: CallOutcome) -> UrgencyTier:
    if fit < 20:
        return "skip"
    if replaceability >= 70:
        return "hot"
    if replaceability >= 45:
        return "warm"
    if outcome != CallOutcome.ANSWERED:
        # Non-answered with mid replaceability — still worth queuing.
        return "warm"
    return "cold"


def _one_liner(ext: Extraction, outcome: CallOutcome, tier: UrgencyTier, reasons: list[str]) -> str:
    """The line an SDR sees first. Keep it under ~140 chars."""
    if outcome == CallOutcome.NO_ANSWER:
        return f"[{tier.upper()}] No one answered during business hours — they're losing orders right now."
    if outcome == CallOutcome.BUSY:
        return f"[{tier.upper()}] Line busy during business hours — likely understaffed phones."
    if outcome == CallOutcome.VOICEMAIL:
        return f"[{tier.upper()}] Phone goes to voicemail mid-day; clear case for AI answering."
    if outcome == CallOutcome.FAILED:
        return "[SKIP] Number appears invalid — verify before outreach."
    if ext.order_refused_reason == "online_only":
        return "[SKIP] Online-only ordering. Maple's phone product doesn't fit."
    if ext.ivr_present and not ext.order_accepted:
        return f"[{tier.upper()}] IVR with no human handoff — caller abandoned. Big Maple win."
    if ext.order_accepted and ext.upsell_attempted and ext.friendliness == "warm":
        return f"[{tier.upper()}] Strong human host: warm, on-script, upsells. Hard sell unless they want to scale."
    if ext.english_fluency in ("partial", "limited"):
        return f"[{tier.upper()}] Language barrier on the phone — Maple can lift order capture without staffing changes."
    if reasons:
        return f"[{tier.upper()}] {reasons[0]}"
    return f"[{tier.upper()}] See breakdown."


def score(ext: Extraction, outcome: CallOutcome) -> Score:
    fit, fit_r = _fit_score(ext, outcome)
    answer_q, ans_r = _answer_quality(ext, outcome)
    order_h, ord_r = _order_handling(ext, outcome)
    host_q, host_r = _host_quality(ext, outcome)
    pain = _pain_score(answer_q, order_h, host_q, outcome)
    replaceability = round((fit / 100.0) * pain, 1)
    all_reasons = fit_r + ans_r + ord_r + host_r
    tier = _tier(replaceability, fit, outcome)
    return Score(
        fit_score=round(fit, 1),
        pain_score=round(pain, 1),
        replaceability_score=replaceability,
        answer_quality=round(answer_q, 1),
        order_handling=round(order_h, 1),
        host_quality=round(host_q, 1),
        urgency_tier=tier,
        sdr_one_liner=_one_liner(ext, outcome, tier, all_reasons),
        reasons=all_reasons,
    )
