"""Production AI Agent — Part 6 Final Project."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import logging
import sys
import time
from typing import Any, Optional
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from app.auth import verify_api_key
from app.config import settings
from app.cost_guard import (
    ESTIMATED_RESERVE_OUTPUT_TOKENS,
    calculate_cost,
    estimate_tokens,
    reserve_budget,
    settle_budget,
)
from app.rate_limiter import check_rate_limit
from app.redis_client import close_redis, redis_healthy
from utils.mock_llm import ask as llm_ask


class JsonFormatter(logging.Formatter):
    """Format every application log line as a guaranteed single-line valid JSON object."""

    _STANDARD_FIELDS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys())

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "lvl": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", "log"),
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in self._STANDARD_FIELDS or key in payload or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _configure_logging() -> logging.Logger:
    app_logger = logging.getLogger("app")
    app_logger.setLevel(getattr(logging, settings.log_level, logging.INFO))
    app_logger.propagate = False
    app_logger.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    app_logger.addHandler(handler)
    return app_logger


logger = _configure_logging()
START_TIME = time.time()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Manage startup and graceful shutdown without overriding Uvicorn SIGTERM handling."""
    logger.info(
        "Agent starting",
        extra={
            "event": "startup",
            "environment": settings.environment,
            "port": settings.port,
            "rate_limit_per_minute": settings.rate_limit_per_minute,
            "daily_budget_usd": settings.daily_budget_usd,
            "monthly_budget_usd": settings.monthly_budget_usd,
            "redis_configured": bool(settings.redis_url),
        },
    )
    try:
        yield
    finally:
        logger.info("Agent shutting down cleanly", extra={"event": "shutdown"})
        close_redis()


app = FastAPI(
    title="Production AI Agent",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url=None,
)


@app.middleware("http")
async def structured_request_logging(request: Request, call_next):
    """Emit structured JSON request logs and attach X-Request-ID."""
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    request.state.request_id = request_id
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:
        route = request.scope.get("route")
        logger.error(
            "Unhandled server exception",
            extra={
                "event": "unhandled_error",
                "request_id": request_id,
                "path": route.path if route else "unmatched",
                "exception_type": type(exc).__name__,
            },
        )
        response = JSONResponse(status_code=500, content={"detail": "Internal server error"})

    route = request.scope.get("route")
    logger.info(
        "HTTP request completed",
        extra={
            "event": "http_request",
            "request_id": request_id,
            "method": request.method,
            "path": route.path if route else "unmatched",
            "status": response.status_code,
            "ms": round((time.perf_counter() - started) * 1000, 2),
        },
    )
    response.headers["X-Request-ID"] = request_id
    return response


class AskRequest(BaseModel):
    """Input schema for POST /ask."""

    question: str = Field(..., min_length=1, max_length=settings.max_question_chars)
    user_id: Optional[str] = Field(default=None, max_length=128)

    @field_validator("question")
    @classmethod
    def question_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question must not be empty or whitespace")
        return value


@app.get("/health")
def health():
    """Liveness probe: returns 200 whenever process is alive, regardless of Redis."""
    return {
        "status": "ok",
        "environment": settings.environment,
        "uptime_seconds": round(time.time() - START_TIME, 1),
    }


@app.get("/ready")
async def ready():
    """Readiness probe: returns 200 when Redis is reachable, 503 otherwise."""
    is_ok = await asyncio.to_thread(redis_healthy)
    if not is_ok:
        raise HTTPException(status_code=503, detail="Redis unavailable")
    return {"ready": True, "redis": "ok"}


@app.post("/ask")
async def ask(
    body: AskRequest,
    request: Request,
    api_key: str = Depends(verify_api_key),
):
    """Authenticated, rate-limited, and budget-guarded AI agent endpoint."""
    await asyncio.to_thread(check_rate_limit, api_key)

    input_tokens = estimate_tokens(body.question)
    reserved_cost = calculate_cost(input_tokens, ESTIMATED_RESERVE_OUTPUT_TOKENS)
    reservation = await asyncio.to_thread(reserve_budget, reserved_cost)

    try:
        answer = await asyncio.to_thread(llm_ask, body.question)
    except Exception:
        try:
            await asyncio.to_thread(settle_budget, reservation, 0.0)
        except HTTPException:
            pass
        raise

    output_tokens = estimate_tokens(answer)
    actual_cost = calculate_cost(input_tokens, output_tokens)
    await asyncio.to_thread(settle_budget, reservation, actual_cost)

    logger.info(
        "Question answered",
        extra={
            "event": "ask_completed",
            "request_id": getattr(request.state, "request_id", None),
            "user_id": body.user_id,
            "question_chars": len(body.question),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "mock_cost_usd": round(actual_cost, 8),
        },
    )

    return {
        "question": body.question,
        "answer": answer,
        "user_id": body.user_id,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_usd": round(actual_cost, 8),
            "pricing_mode": "mock",
        },
    }
