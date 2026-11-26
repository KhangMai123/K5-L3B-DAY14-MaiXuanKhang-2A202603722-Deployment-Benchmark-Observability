"""
ADVANCED — Stateless Agent với Redis Session

Stateless = agent không giữ state trong memory.
Mọi state (session, conversation history) lưu trong Redis.

Tại sao stateless quan trọng khi scale?
  Instance 1: User A gửi request 1 → lưu session trong memory
  Instance 2: User A gửi request 2 → KHÔNG có session! Bug!

  ✅ Giải pháp: Lưu session trong Redis
  Bất kỳ instance nào cũng đọc được session của user.

Demo:
  docker compose up
  # Sau đó test multi-turn conversation
  python test_stateless.py
"""
import os
import time
import json
import logging
import uuid
from datetime import datetime, timezone
from contextlib import asynccontextmanager


import redis
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import uvicorn
from utils.mock_llm import ask

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
_redis = redis.Redis.from_url(
    REDIS_URL, decode_responses=True, socket_timeout=1, socket_connect_timeout=1
)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

START_TIME = time.time()
INSTANCE_ID = os.getenv("INSTANCE_ID", f"instance-{uuid.uuid4().hex[:6]}")


# ──────────────────────────────────────────────────────────
# Session Storage (Redis-backed, Stateless-compatible)
# ──────────────────────────────────────────────────────────

def load_session(session_id: str) -> dict:
    """Load session từ Redis."""
    data = _redis.get(f"session:{session_id}")
    return json.loads(data) if data else {}


def save_turn(session_id: str, question: str, answer: str) -> int:
    """Lưu một lượt hỏi đáp và tăng số lượt bằng cùng một giao dịch Redis."""
    key = f"session:{session_id}"
    timestamp = datetime.now(timezone.utc).isoformat()
    messages = [
        {"role": "user", "content": question, "timestamp": timestamp},
        {"role": "assistant", "content": answer, "timestamp": timestamp},
    ]

    def update(pipe):
        raw = pipe.get(key)
        session = json.loads(raw) if raw else {}
        history = session.get("history", [])
        turn = session.get("turn")
        if turn is None:
            turn = sum(message["role"] == "user" for message in history)
        turn += 1
        session.update(history=(history + messages)[-20:], turn=turn)
        pipe.multi()
        pipe.setex(key, 3600, json.dumps(session))
        return turn

    return _redis.transaction(update, key, value_from_callable=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting instance {INSTANCE_ID}")
    try:
        yield
    finally:
        _redis.close()
        logger.info(f"Instance {INSTANCE_ID} shutting down")


app = FastAPI(
    title="Stateless Agent",
    version="4.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(redis.RedisError)
async def redis_unavailable(_request, _error):
    return JSONResponse(status_code=503, content={"detail": "Redis not available"})


# ──────────────────────────────────────────────────────────
# Models
# ──────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    question: str
    session_id: str | None = None  # None = tạo session mới


# ──────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────

@app.post("/chat")
def chat(body: ChatRequest):
    """
    Multi-turn conversation với session management.

    Gửi session_id trong các request tiếp theo để tiếp tục cuộc trò chuyện.
    Agent có thể chạy trên bất kỳ instance nào — state trong Redis.
    """
    # Tạo hoặc dùng session hiện có
    session_id = body.session_id or str(uuid.uuid4())

    answer = ask(body.question)
    turn = save_turn(session_id, body.question, answer)

    return {
        "session_id": session_id,
        "question": body.question,
        "answer": answer,
        "turn": turn,
        "served_by": INSTANCE_ID,  # ← thấy rõ bất kỳ instance nào cũng serve được
        "storage": "redis",
    }


@app.get("/chat/{session_id}/history")
def get_history(session_id: str):
    """Xem conversation history của một session."""
    session = load_session(session_id)
    if not session:
        raise HTTPException(404, f"Session {session_id} not found or expired")
    return {
        "session_id": session_id,
        "messages": session.get("history", []),
        "count": len(session.get("history", [])),
    }


@app.delete("/chat/{session_id}")
def delete_session(session_id: str):
    """Xóa session (user logout)."""
    _redis.delete(f"session:{session_id}")
    return {"deleted": session_id}


# ──────────────────────────────────────────────────────────
# Health / Metrics
# ──────────────────────────────────────────────────────────

@app.get("/health")
def health():
    try:
        redis_ok = bool(_redis.ping())
    except redis.RedisError:
        redis_ok = False

    return {
        "status": "ok" if redis_ok else "degraded",
        "instance_id": INSTANCE_ID,
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "storage": "redis",
        "redis_connected": redis_ok,
    }


@app.get("/ready")
def ready():
    if not _redis.ping():
        raise HTTPException(503, "Redis not available")
    return {"ready": True, "instance": INSTANCE_ID}


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
