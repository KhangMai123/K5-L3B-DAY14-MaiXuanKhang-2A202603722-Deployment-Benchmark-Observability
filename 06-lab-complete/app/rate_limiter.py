"""Redis-backed atomic sliding-window rate limiter."""
from __future__ import annotations

import hashlib
import time
import uuid

from fastapi import HTTPException
from redis.exceptions import RedisError

from app.config import settings
from app.redis_client import get_redis

_WINDOW_SECONDS = 60

# Atomic sliding-window check and record in Redis sorted set.
_SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local current = redis.call('ZCARD', key)
if current >= limit then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry_after = window
    if oldest[2] ~= nil then
        retry_after = math.max(1, math.ceil((tonumber(oldest[2]) + window) - now))
    end
    return {0, current, retry_after}
end

redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, math.ceil(window) + 5)
return {1, current + 1, 0}
"""


def _rate_limit_key(api_key: str) -> str:
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:24]
    return f"ratelimit:{digest}"


def check_rate_limit(key: str) -> dict[str, int]:
    """Enforce RATE_LIMIT_PER_MINUTE over a 60s sliding window in Redis."""
    client = get_redis()
    if client is None:
        raise HTTPException(status_code=503, detail="Rate limiter backend unavailable")

    limit = settings.rate_limit_per_minute
    now = time.time()
    member = f"{time.time_ns()}:{uuid.uuid4().hex}"
    redis_key = _rate_limit_key(key)

    try:
        allowed, count, retry_after = client.eval(
            _SLIDING_WINDOW_LUA,
            1,
            redis_key,
            f"{now:.6f}",
            str(_WINDOW_SECONDS),
            str(limit),
            member,
        )
    except (RedisError, OSError) as exc:
        raise HTTPException(status_code=503, detail="Rate limiter backend unavailable") from exc

    allowed = int(allowed)
    count = int(count)
    retry_after = int(retry_after)

    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded",
            headers={
                "Retry-After": str(retry_after),
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": "0",
            },
        )

    return {
        "limit": limit,
        "remaining": max(0, limit - count),
        "window_seconds": _WINDOW_SECONDS,
    }
