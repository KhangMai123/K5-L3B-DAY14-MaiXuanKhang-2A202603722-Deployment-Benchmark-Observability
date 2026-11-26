"""Lazy Redis client and lifecycle helpers shared across modules."""
from __future__ import annotations

import threading
from typing import Optional

import redis
from redis.exceptions import RedisError

import app.config as config_module

_lock = threading.Lock()
_client: Optional[redis.Redis] = None


def get_redis(cfg: Optional[config_module.Settings] = None) -> Optional[redis.Redis]:
    """Return a lazy Redis client without connecting during module import."""
    global _client
    if _client is not None:
        return _client

    active_cfg = cfg or config_module.settings
    if not active_cfg.redis_url:
        return None

    with _lock:
        if _client is None:
            try:
                pool = redis.ConnectionPool.from_url(active_cfg.redis_url)
            except (RedisError, OSError, ValueError):
                return None
            # URL options take precedence over kwargs in from_url. Set these before connecting.
            pool.connection_kwargs.update(
                decode_responses=True,
                socket_connect_timeout=active_cfg.redis_timeout_seconds,
                socket_timeout=active_cfg.redis_timeout_seconds,
                retry_on_timeout=False,
            )
            _client = redis.Redis.from_pool(pool)
    return _client


def set_redis_client(client: Optional[redis.Redis]) -> None:
    """Override the singleton Redis client (intended for automated tests)."""
    global _client
    with _lock:
        _client = client


def close_redis() -> None:
    """Close the active Redis client cleanly during application shutdown."""
    global _client
    with _lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:
                pass
            finally:
                _client = None


def redis_healthy(cfg: Optional[config_module.Settings] = None) -> bool:
    """Ping Redis with finite timeout; return False when unconfigured or unreachable."""
    client = get_redis(cfg)
    if client is None:
        return False
    try:
        return bool(client.ping())
    except (RedisError, OSError, ValueError):
        return False
