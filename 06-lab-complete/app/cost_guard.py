"""Redis-backed atomic cost guard with reservation and settlement."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from redis.exceptions import RedisError

from app.config import settings
from app.redis_client import get_redis

# Mock pricing USD / 1K tokens (reference gpt-4o-mini).
PRICE_INPUT_PER_1K = 0.00015
PRICE_OUTPUT_PER_1K = 0.0006

# Conservative mock output token estimate used for pre-call budget reservation.
ESTIMATED_RESERVE_OUTPUT_TOKENS = 32
_DAILY_TTL_SECONDS = 48 * 3600
_MONTHLY_TTL_SECONDS = 35 * 24 * 3600


@dataclass(frozen=True)
class BudgetReservation:
    amount_usd: float
    periods: tuple[tuple[str, float, int], ...]


# Atomically verify daily (and optional monthly) budget and reserve estimated cost.
_RESERVE_BUDGET_LUA = """
local count = tonumber(ARGV[1])
local amount = tonumber(ARGV[2])

for i = 1, count do
    local current = tonumber(redis.call('GET', KEYS[i]) or '0')
    local limit = tonumber(ARGV[2 + i])
    if tonumber(string.format('%.10f', current + amount)) > limit then
        return {0, i, string.format('%.10f', current)}
    end
end

local last_total = 0
for i = 1, count do
    local next_val = redis.call('INCRBYFLOAT', KEYS[i], amount)
    redis.call('EXPIRE', KEYS[i], tonumber(ARGV[2 + count + i]))
    last_total = next_val
end

return {1, 0, tostring(last_total)}
"""

# Atomically settle the actual cost, or refund if an overage would exceed a limit.
_SETTLE_BUDGET_LUA = """
local count = tonumber(ARGV[1])
local reserved = tonumber(ARGV[2])
local delta = tonumber(ARGV[3]) - reserved
local allowed = 1
if delta > 0 then
    for i = 1, count do
        local current = tonumber(redis.call('GET', KEYS[i]) or '0')
        local limit = tonumber(ARGV[3 + i])
        if tonumber(string.format('%.10f', current + delta)) > limit then
            delta = -reserved
            allowed = 0
            break
        end
    end
end

for i = 1, count do
    local current = tonumber(redis.call('GET', KEYS[i]) or '0')
    local updated = math.max(0, current + delta)
    redis.call('SET', KEYS[i], string.format('%.10f', updated), 'EX', tonumber(ARGV[3 + count + i]))
end
return allowed
"""


def estimate_tokens(text: str) -> int:
    """Estimate token count (~4 characters/token) for mock cost accounting."""
    return max(1, (len(text) + 3) // 4)


def calculate_cost(input_tokens: int, output_tokens: int) -> float:
    """Calculate simulated USD cost from token counts."""
    return (input_tokens / 1000.0) * PRICE_INPUT_PER_1K + (output_tokens / 1000.0) * PRICE_OUTPUT_PER_1K


def _now_in_budget_tz() -> datetime:
    if settings.budget_timezone.upper() == "UTC":
        return datetime.now(timezone.utc)
    return datetime.now(ZoneInfo(settings.budget_timezone))


def _period_specs() -> list[tuple[str, float, int]]:
    now = _now_in_budget_tz()
    specs: list[tuple[str, float, int]] = [
        (f"cost:daily:{now.strftime('%Y-%m-%d')}", settings.daily_budget_usd, _DAILY_TTL_SECONDS),
    ]
    if settings.monthly_budget_usd is not None:
        specs.append(
            (f"cost:monthly:{now.strftime('%Y-%m')}", settings.monthly_budget_usd, _MONTHLY_TTL_SECONDS)
        )
    return specs


def reserve_budget(estimated_cost_usd: float) -> BudgetReservation:
    """Atomically reserve estimated cost before invoking the LLM; raise 402 if exceeded."""
    client = get_redis()
    if client is None:
        raise HTTPException(status_code=503, detail="Cost guard backend unavailable")

    specs = _period_specs()
    keys = [s[0] for s in specs]
    limits = [f"{s[1]:.10f}" for s in specs]
    ttls = [str(s[2]) for s in specs]
    reserve = max(0.0, float(estimated_cost_usd))

    try:
        res = client.eval(
            _RESERVE_BUDGET_LUA,
            len(keys),
            *keys,
            str(len(keys)),
            f"{reserve:.10f}",
            *limits,
            *ttls,
        )
    except (RedisError, OSError) as exc:
        raise HTTPException(status_code=503, detail="Cost guard backend unavailable") from exc

    allowed = int(res[0])
    if not allowed:
        raise HTTPException(status_code=402, detail="Budget exceeded")
    return BudgetReservation(reserve, tuple(specs))


def settle_budget(reservation: BudgetReservation, actual_usd: float) -> float:
    """Adjust reserved budget to actual cost after LLM completion (or refund on error)."""
    client = get_redis()
    if client is None:
        raise HTTPException(status_code=503, detail="Cost guard backend unavailable")

    specs = reservation.periods
    keys = [s[0] for s in specs]
    limits = [f"{s[1]:.10f}" for s in specs]
    ttls = [str(s[2]) for s in specs]

    try:
        allowed = client.eval(
            _SETTLE_BUDGET_LUA,
            len(keys),
            *keys,
            str(len(keys)),
            f"{reservation.amount_usd:.10f}",
            f"{actual_usd:.10f}",
            *limits,
            *ttls,
        )
    except (RedisError, OSError) as exc:
        raise HTTPException(status_code=503, detail="Cost guard backend unavailable") from exc
    if not allowed:
        raise HTTPException(status_code=402, detail="Budget exceeded")
    return actual_usd


def check_budget(estimated_cost_usd: float = 0.0) -> None:
    """Verify current spend in Redis allows an upcoming call; raise 402 if exhausted."""
    client = get_redis()
    if client is None:
        raise HTTPException(status_code=503, detail="Cost guard backend unavailable")

    specs = _period_specs()
    try:
        values = client.mget([s[0] for s in specs])
    except (RedisError, OSError) as exc:
        raise HTTPException(status_code=503, detail="Cost guard backend unavailable") from exc

    for ( _, limit, _ ), raw_val in zip(specs, values):
        current = float(raw_val) if raw_val is not None else 0.0
        if current + estimated_cost_usd > limit or current >= limit:
            raise HTTPException(status_code=402, detail="Budget exceeded")


def record_cost(input_tokens: int, output_tokens: int) -> float:
    """Add actual request cost to Redis daily and monthly counters."""
    client = get_redis()
    if client is None:
        raise HTTPException(status_code=503, detail="Cost guard backend unavailable")

    cost = calculate_cost(input_tokens, output_tokens)
    specs = _period_specs()
    try:
        pipe = client.pipeline()
        for key, _, ttl in specs:
            pipe.incrbyfloat(key, cost)
            pipe.expire(key, ttl)
        pipe.execute()
    except (RedisError, OSError) as exc:
        raise HTTPException(status_code=503, detail="Cost guard backend unavailable") from exc
    return cost


def get_current_spend() -> dict[str, Optional[float]]:
    """Return current simulated spend totals from Redis for diagnostics."""
    client = get_redis()
    if client is None:
        return {"daily_usd": None, "monthly_usd": None}
    specs = _period_specs()
    values = client.mget([s[0] for s in specs])
    daily = float(values[0]) if values[0] is not None else 0.0
    monthly = float(values[1]) if len(values) > 1 and values[1] is not None else None
    return {"daily_usd": round(daily, 8), "monthly_usd": round(monthly, 8) if monthly is not None else None}
