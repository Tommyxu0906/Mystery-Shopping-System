"""Transcript → structured extraction. Descriptive fields only. Scoring is separate.

Two implementations:
  - LLMExtractor: uses OpenAI structured outputs when OPENAI_API_KEY is set.
  - HeuristicExtractor: regex/keyword fallback. Always runs as a baseline so the system
    works zero-config and so we have a deterministic signal in tests.

The HeuristicExtractor is intentionally tailored to the mock provider's templates. In real
production you'd never ship it — the LLM is the primary path and the heuristic is a sanity
check / cost guardrail."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Literal

from .config import Config
from .providers.base import CallOutcome

logger = logging.getLogger(__name__)


def _is_blank(s: str | None) -> bool:
    """True if a transcript is effectively empty — None, whitespace, or a single bracketed marker.
    Vapi occasionally hands back a call object with no transcript even when status='ended'."""
    if not s:
        return True
    stripped = s.strip()
    return not stripped or stripped in ("[]", "[empty]", "[no transcript]")


Fluency = Literal["fluent", "partial", "limited", "unknown"]
Friendliness = Literal["warm", "neutral", "curt", "rude", "unknown"]
HungUpBy = Literal["customer", "host", "system", "unknown"]
RefusedReason = Literal["no_takeout", "online_only", "dine_in_only", "closed", "other"]


@dataclass
class Extraction:
    answered_by_human: bool | None
    voicemail: bool
    ivr_present: bool
    hold_time_seconds: int | None
    stated_close_time: str | None
    stated_open_for_takeout: bool | None
    order_accepted: bool
    order_refused_reason: RefusedReason | None
    upsell_attempted: bool
    estimated_pickup_minutes: int | None
    host_name: str | None
    english_fluency: Fluency
    friendliness: Friendliness
    hung_up_by: HungUpBy
    notable_quotes: list[str] = field(default_factory=list)
    summary_one_line: str = ""
    extractor: str = ""  # "llm" | "heuristic"

    def to_dict(self) -> dict:
        return asdict(self)


def _empty_for_non_answered(outcome: CallOutcome, extractor: str, summary: str) -> Extraction:
    return Extraction(
        answered_by_human=False if outcome != CallOutcome.ANSWERED else None,
        voicemail=outcome == CallOutcome.VOICEMAIL,
        ivr_present=False,
        hold_time_seconds=None,
        stated_close_time=None,
        stated_open_for_takeout=None,
        order_accepted=False,
        order_refused_reason=None,
        upsell_attempted=False,
        estimated_pickup_minutes=None,
        host_name=None,
        english_fluency="unknown",
        friendliness="unknown",
        hung_up_by="system" if outcome in (CallOutcome.NO_ANSWER, CallOutcome.BUSY, CallOutcome.FAILED) else "unknown",
        summary_one_line=summary,
        extractor=extractor,
    )


# ---------------- Heuristic ----------------

_CLOSE_TIME_RE = re.compile(
    r"(?:open until|close(?:s|d)? at|until|til|till|open till)\s*"
    r"(\d{1,2}(?::\d{2})?\s*(?:am|pm|AM|PM|a\.m\.|p\.m\.)?|\d{1,2})",
    re.IGNORECASE,
)
_PICKUP_RE = re.compile(r"(?:ready in|takes?|about|around)\s*(?:about\s+)?(\d{1,3})\s*minutes?", re.IGNORECASE)
_HOLD_RE = re.compile(r"hold music for[^\d]{0,15}(\d{1,3})", re.IGNORECASE)
_HOST_NAME_RE = re.compile(r"this is\s+([A-Z][a-z]+)", re.IGNORECASE)
_UPSELL_KEYWORDS = ("would you like", "goes well", "add a", "want to add", "with that", "anything else", "how about")
_REFUSAL_PATTERNS: list[tuple[str, RefusedReason]] = [
    ("through our website", "online_only"),
    ("only do takeout through", "online_only"),
    ("doordash", "online_only"),
    ("dine-in only", "dine_in_only"),
    ("dine in only", "dine_in_only"),
    ("we're closed", "closed"),
    ("not tonight", "no_takeout"),
    ("no takeout", "no_takeout"),
]


def _detect_fluency(transcript: str) -> Fluency:
    if re.search(r"\bsorry\b.*\bmoment\b|my son help|one moment, my son", transcript, re.IGNORECASE):
        return "limited"
    if re.search(r"eh… |Eh… |\.\.\.|takeout, yes\. You order", transcript):
        return "partial"
    return "fluent"


def _detect_friendliness(transcript: str) -> Friendliness:
    if re.search(r"hold please|sorry about that", transcript, re.IGNORECASE):
        return "curt"
    if re.search(r"\bup to you\b|Yeah, til", transcript, re.IGNORECASE):
        return "neutral"
    if re.search(r"absolutely|perfect|no problem|happy to|thanks so much|thank you for calling", transcript, re.IGNORECASE):
        return "warm"
    return "neutral"


class HeuristicExtractor:
    name = "heuristic"

    def extract(self, transcript: str, outcome: CallOutcome) -> Extraction:
        # Edge case: provider reported 'answered' but the transcript came back empty. This
        # happens in real life — Vapi can lose audio capture or the agent can hang up before
        # speech is recorded. Treat as a degraded result so we don't pretend to have data.
        if outcome == CallOutcome.ANSWERED and _is_blank(transcript):
            ext = _empty_for_non_answered(outcome, self.name,
                "Call connected but no transcript captured — likely audio/capture failure.")
            ext.answered_by_human = True  # we know that much
            return ext

        if outcome == CallOutcome.VOICEMAIL:
            close_m = _CLOSE_TIME_RE.search(transcript)
            return Extraction(
                answered_by_human=False, voicemail=True, ivr_present=False,
                hold_time_seconds=None,
                stated_close_time=close_m.group(1).strip() if close_m else None,
                stated_open_for_takeout=None, order_accepted=False,
                order_refused_reason=None, upsell_attempted=False,
                estimated_pickup_minutes=None, host_name=None,
                english_fluency="unknown", friendliness="unknown",
                hung_up_by="customer", notable_quotes=[],
                summary_one_line="Reached voicemail; hours stated in greeting.",
                extractor=self.name,
            )
        if outcome == CallOutcome.NO_ANSWER:
            return _empty_for_non_answered(outcome, self.name, "No answer after ringing.")
        if outcome == CallOutcome.BUSY:
            return _empty_for_non_answered(outcome, self.name, "Line was busy.")
        if outcome == CallOutcome.FAILED:
            return _empty_for_non_answered(outcome, self.name, "Carrier or system error placing the call.")

        ivr_present = "IVR:" in transcript or "press 1" in transcript.lower()
        if ivr_present and "[Alex hangs up" in transcript:
            return Extraction(
                answered_by_human=False, voicemail=False, ivr_present=True,
                hold_time_seconds=60, stated_close_time=None,
                stated_open_for_takeout=None, order_accepted=False,
                order_refused_reason="other", upsell_attempted=False,
                estimated_pickup_minutes=None, host_name=None,
                english_fluency="unknown", friendliness="unknown",
                hung_up_by="customer",
                notable_quotes=["IVR with no path to a human; abandoned after >60s hold."],
                summary_one_line="IVR with no human handoff; caller abandoned.",
                extractor=self.name,
            )

        refused: RefusedReason | None = None
        for needle, reason in _REFUSAL_PATTERNS:
            if needle in transcript.lower():
                refused = reason
                break

        close_m = _CLOSE_TIME_RE.search(transcript)
        pickup_m = _PICKUP_RE.search(transcript)
        hold_m = _HOLD_RE.search(transcript)
        host_m = _HOST_NAME_RE.search(transcript)

        upsell = any(kw in transcript.lower() for kw in _UPSELL_KEYWORDS)
        order_accepted = refused is None and bool(pickup_m or "Should be ready" in transcript or "Okay, name?" in transcript)

        # Friendly host turn(s) for notable quotes.
        notable = []
        for m in re.finditer(r"Host:\s+(.+)", transcript):
            line = m.group(1).strip()
            if any(k in line.lower() for k in ("would you like", "goes really well", "no problem", "popular", "we're dine-in", "only do takeout through", "hold please")):
                notable.append(line)
            if len(notable) >= 3:
                break

        summary_bits = []
        if refused:
            summary_bits.append(f"Order refused: {refused}.")
        else:
            summary_bits.append("Order takeable on the phone.")
            if upsell:
                summary_bits.append("Host attempted an upsell.")
        if hold_m:
            summary_bits.append(f"~{hold_m.group(1)}s hold before being helped.")

        return Extraction(
            answered_by_human=True,
            voicemail=False,
            ivr_present=ivr_present,
            hold_time_seconds=int(hold_m.group(1)) if hold_m else 0,
            stated_close_time=close_m.group(1).strip() if close_m else None,
            stated_open_for_takeout=refused is None,
            order_accepted=order_accepted,
            order_refused_reason=refused,
            upsell_attempted=upsell,
            estimated_pickup_minutes=int(pickup_m.group(1)) if pickup_m else None,
            host_name=host_m.group(1) if host_m else None,
            english_fluency=_detect_fluency(transcript),
            friendliness=_detect_friendliness(transcript),
            hung_up_by="customer",
            notable_quotes=notable,
            summary_one_line=" ".join(summary_bits),
            extractor=self.name,
        )


# ---------------- LLM ----------------

_LLM_SYSTEM = """You extract structured facts from a phone-call transcript between a customer and a \
restaurant host. You are DESCRIPTIVE: you record what happened, not what was good or bad. Scoring \
is a separate step. Output strictly the JSON schema requested. If a field is not knowable from the \
transcript, use null (or the documented sentinel like "unknown")."""

_LLM_JSON_SCHEMA = {
    "name": "CallExtraction",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "answered_by_human": {"type": ["boolean", "null"]},
            "voicemail": {"type": "boolean"},
            "ivr_present": {"type": "boolean"},
            "hold_time_seconds": {"type": ["integer", "null"]},
            "stated_close_time": {"type": ["string", "null"]},
            "stated_open_for_takeout": {"type": ["boolean", "null"]},
            "order_accepted": {"type": "boolean"},
            "order_refused_reason": {
                "type": ["string", "null"],
                "enum": ["no_takeout", "online_only", "dine_in_only", "closed", "other", None],
            },
            "upsell_attempted": {"type": "boolean"},
            "estimated_pickup_minutes": {"type": ["integer", "null"]},
            "host_name": {"type": ["string", "null"]},
            "english_fluency": {"type": "string", "enum": ["fluent", "partial", "limited", "unknown"]},
            "friendliness": {"type": "string", "enum": ["warm", "neutral", "curt", "rude", "unknown"]},
            "hung_up_by": {"type": "string", "enum": ["customer", "host", "system", "unknown"]},
            "notable_quotes": {"type": "array", "items": {"type": "string"}},
            "summary_one_line": {"type": "string"},
        },
        "required": [
            "answered_by_human", "voicemail", "ivr_present", "hold_time_seconds",
            "stated_close_time", "stated_open_for_takeout", "order_accepted",
            "order_refused_reason", "upsell_attempted", "estimated_pickup_minutes",
            "host_name", "english_fluency", "friendliness", "hung_up_by",
            "notable_quotes", "summary_one_line",
        ],
    },
    "strict": True,
}


class LLMExtractor:
    name = "llm"

    def __init__(self, api_key: str, model: str = "gpt-4o-mini"):
        from openai import OpenAI
        self._client = OpenAI(api_key=api_key)
        self._model = model

    def _fallback(self, transcript: str, outcome: CallOutcome, reason: str) -> Extraction:
        """Drop down to heuristic, but stamp the extractor name so the SDR/UI knows what happened."""
        logger.warning("LLM extraction fell back to heuristic: %s", reason)
        ext = HeuristicExtractor().extract(transcript, outcome)
        ext.extractor = "heuristic_llm_fallback"
        return ext

    def extract(self, transcript: str, outcome: CallOutcome) -> Extraction:
        # Non-answered outcomes don't need an LLM call — saves money on no-answers.
        if outcome != CallOutcome.ANSWERED:
            return HeuristicExtractor().extract(transcript, outcome)

        # Same audio/capture failure case as the heuristic — no need to pay OpenAI to look at nothing.
        if _is_blank(transcript):
            ext = _empty_for_non_answered(outcome, self.name,
                "Call connected but no transcript captured — likely audio/capture failure.")
            ext.answered_by_human = True
            return ext

        user_msg = (
            f"Call outcome: {outcome.value}\n"
            f"Transcript:\n---\n{transcript}\n---\n"
            "Return the JSON object matching the CallExtraction schema."
        )
        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _LLM_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                response_format={"type": "json_schema", "json_schema": _LLM_JSON_SCHEMA},
                temperature=0,
            )
        except Exception as exc:
            # Network errors, rate limits, auth errors — all bucket here. Heuristic catches us.
            return self._fallback(transcript, outcome, f"OpenAI API error: {type(exc).__name__}: {exc}")

        raw = (resp.choices[0].message.content or "").strip()
        if not raw:
            return self._fallback(transcript, outcome, "LLM returned empty content")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            return self._fallback(transcript, outcome, f"LLM returned invalid JSON: {exc}")

        try:
            data["extractor"] = self.name
            return Extraction(**data)
        except TypeError as exc:
            # Model returned fields that don't match our dataclass — schema drift.
            return self._fallback(transcript, outcome, f"LLM JSON didn't match schema: {exc}")


def build_extractor(cfg: Config):
    if cfg.openai_api_key:
        return LLMExtractor(cfg.openai_api_key)
    return HeuristicExtractor()
