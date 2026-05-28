"""Vapi adapter. Stubbed against the real API surface so swapping from mock is a config flag,
not a refactor. See https://docs.vapi.ai/api-reference/calls/create.

This file intentionally does NOT actually place calls when credentials are missing — it raises
clearly. The mock provider is the default in .env.example. To switch:
    CALL_PROVIDER=vapi
    VAPI_API_KEY=...
    VAPI_PHONE_NUMBER_ID=...   # phone number registered in your Vapi dashboard
    VAPI_ASSISTANT_ID=...      # optional; we send an inline assistant otherwise
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from ..agent_script import opening_line
from .base import CallOutcome, CallRequest, CallResult

_VAPI_BASE = "https://api.vapi.ai"
_TERMINAL_STATUSES = {"ended", "completed", "failed", "no-answer", "busy"}


def _map_outcome(vapi_status: str, ended_reason: str | None) -> CallOutcome:
    """Translate Vapi's status / endedReason vocabulary into our CallOutcome enum."""
    if vapi_status in ("no-answer",) or ended_reason in ("customer-did-not-answer", "no-answer"):
        return CallOutcome.NO_ANSWER
    if vapi_status == "busy" or ended_reason == "customer-busy":
        return CallOutcome.BUSY
    if ended_reason in ("voicemail", "customer-voicemail"):
        return CallOutcome.VOICEMAIL
    if vapi_status == "failed" or (ended_reason and "error" in ended_reason):
        return CallOutcome.FAILED
    return CallOutcome.ANSWERED


class VapiProvider:
    name = "vapi"

    def __init__(
        self,
        api_key: str | None,
        phone_number_id: str | None,
        assistant_id: str | None,
        poll_interval_s: float = 4.0,
        poll_timeout_s: float = 300.0,
    ):
        if not api_key or not phone_number_id:
            raise RuntimeError(
                "VapiProvider needs VAPI_API_KEY and VAPI_PHONE_NUMBER_ID. "
                "Set CALL_PROVIDER=mock or fill those in .env."
            )
        self.api_key = api_key
        self.phone_number_id = phone_number_id
        self.assistant_id = assistant_id
        self.poll_interval_s = poll_interval_s
        self.poll_timeout_s = poll_timeout_s

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _create_payload(self, req: CallRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "phoneNumberId": self.phone_number_id,
            "customer": {"number": req.phone},
        }
        if self.assistant_id:
            payload["assistantId"] = self.assistant_id
            # Override the first line per-restaurant.
            payload["assistantOverrides"] = {"firstMessage": req.opening_line}
        else:
            # Inline assistant lets us ship without pre-configuring one in the dashboard.
            payload["assistant"] = {
                "model": {
                    "provider": "openai",
                    "model": "gpt-4o",
                    "messages": [{"role": "system", "content": req.system_prompt}],
                },
                "voice": {"provider": "11labs", "voiceId": "burt"},
                "firstMessage": req.opening_line or opening_line(),
                "endCallFunctionEnabled": True,
            }
        return payload

    def place_call(self, req: CallRequest) -> CallResult:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(f"{_VAPI_BASE}/call", json=self._create_payload(req), headers=self._headers())
            resp.raise_for_status()
            created = resp.json()
            call_id = created.get("id")
            if not call_id:
                return CallResult(
                    outcome=CallOutcome.FAILED, provider=self.name,
                    transcript="[Vapi did not return a call id]", raw_metadata=created,
                )

            deadline = time.monotonic() + self.poll_timeout_s
            while time.monotonic() < deadline:
                time.sleep(self.poll_interval_s)
                poll = client.get(f"{_VAPI_BASE}/call/{call_id}", headers=self._headers())
                poll.raise_for_status()
                data = poll.json()
                status = data.get("status", "")
                if status in _TERMINAL_STATUSES:
                    outcome = _map_outcome(status, data.get("endedReason"))
                    transcript = data.get("transcript") or data.get("artifact", {}).get("transcript", "")
                    return CallResult(
                        outcome=outcome, provider=self.name, provider_call_id=call_id,
                        duration_seconds=float(data.get("durationSeconds") or 0.0),
                        transcript=transcript, raw_metadata=data,
                    )

            return CallResult(
                outcome=CallOutcome.FAILED, provider=self.name, provider_call_id=call_id,
                transcript="[polling deadline exceeded]", raw_metadata={"timeout": True},
            )
