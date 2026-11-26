"""Configuration management for the Part 6 production AI agent (12-Factor)."""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

# Real OS environment variables take precedence over .env (override=False).
load_dotenv(override=False)

_VALID_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
_VALID_ENVIRONMENTS = {"development", "staging", "production", "test", "local"}


def _parse_int(name: str, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    raw = os.getenv(name)
    value = default if raw is None or raw.strip() == "" else int(raw.strip())
    if min_value is not None and value < min_value:
        raise ValueError(f"{name} must be >= {min_value}, got {value}")
    if max_value is not None and value > max_value:
        raise ValueError(f"{name} must be <= {max_value}, got {value}")
    return value


def _parse_float(name: str, default: float | None, *, min_value: float | None = None) -> float | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    value = float(raw.strip())
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if min_value is not None and value <= min_value:
        raise ValueError(f"{name} must be > {min_value}, got {value}")
    return value


def _parse_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


@dataclass(frozen=True)
class Settings:
    """Validated application settings loaded from environment variables."""

    port: int
    environment: str
    debug: bool
    log_level: str
    agent_api_key: str
    redis_url: str
    redis_timeout_seconds: float
    rate_limit_per_minute: int
    daily_budget_usd: float
    monthly_budget_usd: Optional[float]
    budget_timezone: str
    max_question_chars: int

    @classmethod
    def from_env(cls) -> "Settings":
        """Load and validate settings from current environment variables."""
        port = _parse_int("PORT", 8000, min_value=1, max_value=65535)
        environment = os.getenv("ENVIRONMENT", "development").strip().lower() or "development"
        if environment not in _VALID_ENVIRONMENTS:
            raise ValueError(
                f"ENVIRONMENT must be one of {sorted(_VALID_ENVIRONMENTS)}, got {environment!r}"
            )

        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
        if log_level not in _VALID_LOG_LEVELS:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(_VALID_LOG_LEVELS)}, got {log_level!r}")

        tz_name = os.getenv("BUDGET_TIMEZONE", "UTC").strip() or "UTC"
        if tz_name.upper() != "UTC":
            try:
                ZoneInfo(tz_name)
            except ZoneInfoNotFoundError as exc:
                raise ValueError(f"Invalid BUDGET_TIMEZONE: {tz_name!r}") from exc

        redis_timeout = _parse_float("REDIS_TIMEOUT_SECONDS", 1.5, min_value=0.0)
        assert redis_timeout is not None
        daily_budget = _parse_float("DAILY_BUDGET_USD", 1.0, min_value=0.0)
        assert daily_budget is not None
        monthly_budget = _parse_float("MONTHLY_BUDGET_USD", None, min_value=0.0)

        return cls(
            port=port,
            environment=environment,
            debug=_parse_bool("DEBUG", False),
            log_level=log_level,
            agent_api_key=os.getenv("AGENT_API_KEY", "").strip(),
            redis_url=os.getenv("REDIS_URL", "").strip(),
            redis_timeout_seconds=redis_timeout,
            rate_limit_per_minute=_parse_int("RATE_LIMIT_PER_MINUTE", 10, min_value=1),
            daily_budget_usd=daily_budget,
            monthly_budget_usd=monthly_budget,
            budget_timezone=tz_name,
            max_question_chars=_parse_int("MAX_QUESTION_CHARS", 2000, min_value=1),
        )


settings = Settings.from_env()
