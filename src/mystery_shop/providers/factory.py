"""One place that resolves the provider from config — keeps the orchestrator pure."""
from __future__ import annotations

from ..config import Config
from .base import CallProvider
from .mock import MockProvider
from .vapi import VapiProvider


def build_provider(cfg: Config) -> CallProvider:
    name = cfg.call_provider.lower()
    if name == "mock":
        return MockProvider()
    if name == "vapi":
        return VapiProvider(
            api_key=cfg.vapi_api_key,
            phone_number_id=cfg.vapi_phone_number_id,
            assistant_id=cfg.vapi_assistant_id,
        )
    raise ValueError(f"Unknown CALL_PROVIDER: {cfg.call_provider}")
