"""Provider interface. Anything that can place a phone call and return a transcript
implements this. Mock and Vapi both live behind it; swapping is one config toggle."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class CallOutcome(str, Enum):
    ANSWERED = "answered"
    VOICEMAIL = "voicemail"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    FAILED = "failed"


@dataclass
class CallRequest:
    lead_id: int
    phone: str
    restaurant_name: str | None
    system_prompt: str
    opening_line: str


@dataclass
class CallResult:
    outcome: CallOutcome
    provider: str
    provider_call_id: str | None = None
    duration_seconds: float = 0.0
    transcript: str = ""
    raw_metadata: dict[str, Any] = field(default_factory=dict)


class CallProvider(Protocol):
    name: str
    def place_call(self, req: CallRequest) -> CallResult: ...
