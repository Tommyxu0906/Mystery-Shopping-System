"""Single source of truth for runtime config. Read once from env, don't sprinkle os.getenv calls through the codebase."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


@dataclass(frozen=True)
class Config:
    db_path: Path
    call_provider: str
    openai_api_key: str | None
    vapi_api_key: str | None
    vapi_phone_number_id: str | None
    vapi_assistant_id: str | None
    business_hours_local: tuple[int, int] = (11, 20)  # 11am-8pm restaurant takeout window
    max_attempts_per_lead: int = 3
    retry_after_no_answer_min: int = 120
    retry_after_busy_min: int = 30


def load_config() -> Config:
    return Config(
        db_path=Path(os.getenv("DB_PATH", "./mystery_shop.db")).resolve(),
        call_provider=os.getenv("CALL_PROVIDER", "mock"),
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        vapi_api_key=os.getenv("VAPI_API_KEY") or None,
        vapi_phone_number_id=os.getenv("VAPI_PHONE_NUMBER_ID") or None,
        vapi_assistant_id=os.getenv("VAPI_ASSISTANT_ID") or None,
    )
