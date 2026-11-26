"""Comprehensive tests for Part 6 production agent (using fakeredis[lua] + live Uvicorn)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import importlib
import io
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import fakeredis
from fastapi.testclient import TestClient
import pytest

PART6_DIR = Path(__file__).resolve().parents[1]
if str(PART6_DIR) not in sys.path:
    sys.path.insert(0, str(PART6_DIR))


def _reload_app_with_env(monkeypatch: pytest.MonkeyPatch, env_overrides: dict[str, str], fake_redis):
    for key in [
        "PORT",
        "ENVIRONMENT",
        "DEBUG",
        "LOG_LEVEL",
        "AGENT_API_KEY",
        "REDIS_URL",
        "REDIS_TIMEOUT_SECONDS",
        "RATE_LIMIT_PER_MINUTE",
        "DAILY_BUDGET_USD",
        "MONTHLY_BUDGET_USD",
        "BUDGET_TIMEZONE",
        "MAX_QUESTION_CHARS",
    ]:
        monkeypatch.delenv(key, raising=False)
    for k, v in env_overrides.items():
        monkeypatch.setenv(k, v)

    import app.config as cfg_mod
    import app.redis_client as rc_mod
    import app.auth as auth_mod
    import app.rate_limiter as rl_mod
    import app.cost_guard as cg_mod
    import app.main as main_mod

    importlib.reload(cfg_mod)
    importlib.reload(rc_mod)
    rc_mod.set_redis_client(fake_redis)
    importlib.reload(auth_mod)
    importlib.reload(rl_mod)
    importlib.reload(cg_mod)
    importlib.reload(main_mod)
    return main_mod, rc_mod, cfg_mod


def test_auth_401_missing_wrong_and_unconfigured_key(monkeypatch: pytest.MonkeyPatch):
    server = fakeredis.FakeServer()
    fake = fakeredis.FakeRedis(server=server, decode_responses=True)
    main_mod, _, _ = _reload_app_with_env(
        monkeypatch,
        {"AGENT_API_KEY": "super-secret-test-key", "RATE_LIMIT_PER_MINUTE": "20"},
        fake,
    )
    with TestClient(main_mod.app) as client:
        r_no_key = client.post("/ask", json={"question": "Hello"})
        assert r_no_key.status_code == 401

        r_wrong = client.post("/ask", headers={"X-API-Key": "wrong-key"}, json={"question": "Hello"})
        assert r_wrong.status_code == 401

        r_ok = client.post(
            "/ask",
            headers={"X-API-Key": "super-secret-test-key"},
            json={"question": "What is docker?", "user_id": "u1"},
        )
        assert r_ok.status_code == 200
        data = r_ok.json()
        assert data["question"] == "What is docker?"
        assert isinstance(data["answer"], str) and len(data["answer"]) > 0
        assert data["usage"]["pricing_mode"] == "mock"

    # Empty server AGENT_API_KEY must reject empty or arbitrary X-API-Key with 401.
    main_mod_empty, _, _ = _reload_app_with_env(monkeypatch, {"AGENT_API_KEY": ""}, fake)
    with TestClient(main_mod_empty.app) as client:
        assert client.post("/ask", json={"question": "Hello"}).status_code == 401
        assert client.post("/ask", headers={"X-API-Key": ""}, json={"question": "Hello"}).status_code == 401
        assert client.post("/ask", headers={"X-API-Key": "anything"}, json={"question": "Hello"}).status_code == 401


def test_validation_and_safe_500_without_stacktrace(monkeypatch: pytest.MonkeyPatch):
    server = fakeredis.FakeServer()
    fake = fakeredis.FakeRedis(server=server, decode_responses=True)
    main_mod, _, _ = _reload_app_with_env(
        monkeypatch,
        {"AGENT_API_KEY": "valid-key", "RATE_LIMIT_PER_MINUTE": "50"},
        fake,
    )
    with TestClient(main_mod.app, raise_server_exceptions=False) as client:
        headers = {"X-API-Key": "valid-key"}
        # Missing field
        assert client.post("/ask", headers=headers, json={}).status_code == 422
        # Empty string
        assert client.post("/ask", headers=headers, json={"question": ""}).status_code == 422
        # Whitespace only
        assert client.post("/ask", headers=headers, json={"question": "   \t\n "}).status_code == 422
        # Too long
        assert client.post("/ask", headers=headers, json={"question": "x" * 2001}).status_code == 422
        # Malformed JSON
        r_bad_json = client.post(
            "/ask",
            headers={**headers, "Content-Type": "application/json"},
            content="{broken json",
        )
        assert r_bad_json.status_code == 422
        assert "Traceback" not in r_bad_json.text

        # Force internal LLM failure -> 500 without stack trace + refund reservation
        monkeypatch.setattr(main_mod, "llm_ask", lambda _q: (_ for _ in ()).throw(RuntimeError("boom secret detail")))
        r_err = client.post("/ask", headers=headers, json={"question": "Trigger error"})
        assert r_err.status_code == 500
        assert r_err.json() == {"detail": "Internal server error"}
        assert "Traceback" not in r_err.text
        assert "boom secret detail" not in r_err.text

        from app.cost_guard import get_current_spend
        spend = get_current_spend()
        assert spend["daily_usd"] == 0.0


def test_rate_limit_concurrent_restart_persistence_and_no_secret_in_keys(monkeypatch: pytest.MonkeyPatch):
    server = fakeredis.FakeServer()
    fake = fakeredis.FakeRedis(server=server, decode_responses=True)
    api_key = "key-secret-concurrent-999"
    main_mod, _, _ = _reload_app_with_env(
        monkeypatch,
        {"AGENT_API_KEY": api_key, "RATE_LIMIT_PER_MINUTE": "5", "DAILY_BUDGET_USD": "10"},
        fake,
    )
    monkeypatch.setattr(main_mod, "llm_ask", lambda q: f"mock:{q}")

    with TestClient(main_mod.app) as client:
        def send(idx: int):
            # Vary user_id on every request to ensure client cannot bypass API-key limit.
            return client.post(
                "/ask",
                headers={"X-API-Key": api_key},
                json={"question": f"Q{idx}", "user_id": f"spoof-{idx}"},
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(send, range(12)))

        codes = [r.status_code for r in responses]
        assert codes.count(200) == 5
        assert codes.count(429) == 7
        for r in responses:
            if r.status_code == 429:
                ra = int(r.headers["retry-after"])
                assert 1 <= ra <= 60

    # Ensure raw API key is NOT stored in Redis keys.
    redis_keys = fake.keys("*")
    assert any(k.startswith("ratelimit:") for k in redis_keys)
    assert all(api_key not in k for k in redis_keys)

    # Simulate full app restart sharing the same Redis server: next request is still 429.
    fake2 = fakeredis.FakeRedis(server=server, decode_responses=True)
    main_mod2, _, _ = _reload_app_with_env(
        monkeypatch,
        {"AGENT_API_KEY": api_key, "RATE_LIMIT_PER_MINUTE": "5", "DAILY_BUDGET_USD": "10"},
        fake2,
    )
    with TestClient(main_mod2.app) as client2:
        r_after = client2.post("/ask", headers={"X-API-Key": api_key}, json={"question": "after restart"})
        assert r_after.status_code == 429


def test_cost_guard_atomic_reservation_and_monthly_limit(monkeypatch: pytest.MonkeyPatch):
    server = fakeredis.FakeServer()
    fake = fakeredis.FakeRedis(server=server, decode_responses=True)
    api_key = "cost-test-key"

    # Tiny daily budget -> 402 immediately
    main_mod, _, _ = _reload_app_with_env(
        monkeypatch,
        {
            "AGENT_API_KEY": api_key,
            "RATE_LIMIT_PER_MINUTE": "100",
            "DAILY_BUDGET_USD": "0.000001",
            "MONTHLY_BUDGET_USD": "0.000001",
        },
        fake,
    )
    with TestClient(main_mod.app) as client:
        r = client.post("/ask", headers={"X-API-Key": api_key}, json={"question": "What is deployment?"})
        assert r.status_code == 402

    # Concurrent budget race test: budget allows ~2 requests, ensure total spend never exceeds budget.
    server2 = fakeredis.FakeServer()
    fake2 = fakeredis.FakeRedis(server=server2, decode_responses=True)
    tight_budget = 0.000045
    main_mod2, _, _ = _reload_app_with_env(
        monkeypatch,
        {
            "AGENT_API_KEY": api_key,
            "RATE_LIMIT_PER_MINUTE": "100",
            "DAILY_BUDGET_USD": str(tight_budget),
            "MONTHLY_BUDGET_USD": "1.0",
        },
        fake2,
    )
    monkeypatch.setattr(main_mod2, "llm_ask", lambda _q: "Short ok answer.")

    with TestClient(main_mod2.app) as client2:
        with ThreadPoolExecutor(max_workers=6) as pool:
            resps = list(
                pool.map(
                    lambda i: client2.post(
                        "/ask",
                        headers={"X-API-Key": api_key},
                        json={"question": f"Hello {i}"},
                    ),
                    range(8),
                )
            )
        codes = [r.status_code for r in resps]
        assert 200 in codes
        assert 402 in codes
        from app.cost_guard import get_current_spend
        spend = get_current_spend()
        assert spend["daily_usd"] <= tight_budget + 1e-9
        assert spend["monthly_usd"] is not None and spend["monthly_usd"] > 0


def test_redis_down_and_reconnect(monkeypatch: pytest.MonkeyPatch):
    # No Redis configured -> /health 200, /ready 503, /ask 503
    main_mod, rc_mod, _ = _reload_app_with_env(
        monkeypatch,
        {"AGENT_API_KEY": "k1", "REDIS_URL": ""},
        None,
    )
    with TestClient(main_mod.app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 503
        assert client.post("/ask", headers={"X-API-Key": "k1"}, json={"question": "Hi"}).status_code == 503

        # Simulate Redis coming back online dynamically
        fake = fakeredis.FakeRedis(decode_responses=True)
        rc_mod.set_redis_client(fake)
        assert client.get("/ready").status_code == 200
        assert client.post("/ask", headers={"X-API-Key": "k1"}, json={"question": "Hi"}).status_code == 200


def test_structured_json_logs_quotes_unicode_exception_and_no_secret(monkeypatch: pytest.MonkeyPatch):
    server = fakeredis.FakeServer()
    fake = fakeredis.FakeRedis(server=server, decode_responses=True)
    secret = "ultra-secret-api-key-12345"
    main_mod, _, _ = _reload_app_with_env(
        monkeypatch,
        {"AGENT_API_KEY": secret, "RATE_LIMIT_PER_MINUTE": "20"},
        fake,
    )
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(main_mod.JsonFormatter())
    app_logger = logging.getLogger("app")
    app_logger.handlers.clear()
    app_logger.addHandler(handler)

    with TestClient(main_mod.app) as client:
        client.post(
            "/ask",
            headers={"X-API-Key": secret},
            json={"question": 'Câu hỏi có "ngoặc kép" và Tiếng Việt?'},
        )

    try:
        raise ValueError('Lỗi giả lập với "ngoặc kép" & Unicode')
    except ValueError:
        app_logger.error("Caught test error", exc_info=True, extra={"event": "test_error"})

    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    assert len(lines) >= 2
    for ln in lines:
        parsed = json.loads(ln)
        assert isinstance(parsed, dict)
        assert "ts" in parsed and "lvl" in parsed and "event" in parsed
        assert secret not in ln


def test_env_precedence_over_dotenv_and_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    env_file = tmp_path / ".env"
    env_file.write_text("ENVIRONMENT=development\nRATE_LIMIT_PER_MINUTE=3\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "25")

    from dotenv import load_dotenv
    load_dotenv(dotenv_path=env_file, override=False)

    import app.config as cfg_mod
    importlib.reload(cfg_mod)
    assert cfg_mod.settings.environment == "staging"
    assert cfg_mod.settings.rate_limit_per_minute == 25

    monkeypatch.setenv("PORT", "99999")
    with pytest.raises(ValueError):
        cfg_mod.Settings.from_env()
    monkeypatch.setenv("PORT", "8000")
    monkeypatch.setenv("ENVIRONMENT", "invalid_env")
    with pytest.raises(ValueError):
        cfg_mod.Settings.from_env()


def test_uvicorn_custom_port_startup_without_redis():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    env = os.environ.copy()
    env.pop("REDIS_URL", None)
    env["PORT"] = str(port)
    env["ENVIRONMENT"] = "staging"
    env["AGENT_API_KEY"] = "custom-port-test-key"
    env["PYTHONPATH"] = str(PART6_DIR)
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--workers", "2"],
        cwd=str(PART6_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 15
        healthy = False
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                    if r.status == 200:
                        healthy = True
                        break
            except Exception:
                time.sleep(0.25)
        if not healthy:
            pytest.fail(f"Server failed to become healthy on port {port}")

        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/ready", timeout=5)
            pytest.fail("Expected /ready to return 503 when Redis is not configured")
        except urllib.error.HTTPError as e:
            assert e.code == 503
    finally:
        if proc.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            else:
                proc.terminate()
        output, _ = proc.communicate(timeout=10)
        if not healthy:
            print(output)


@pytest.mark.parametrize(("name", "value"), [
    ("DAILY_BUDGET_USD", "nan"),
    ("MONTHLY_BUDGET_USD", "inf"),
    ("REDIS_TIMEOUT_SECONDS", "inf"),
    ("DEBUG", "treu"),
])
def test_rejects_invalid_runtime_configuration(monkeypatch, name, value):
    from app.config import Settings

    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        Settings.from_env()


def test_invalid_redis_url_makes_agent_unready(monkeypatch):
    app, _, _ = _reload_app_with_env(
        monkeypatch, {"REDIS_URL": "invalid://redis", "AGENT_API_KEY": "local-test-key"}, None
    )
    with TestClient(app.app, raise_server_exceptions=False) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 503
        assert client.post(
            "/ask", headers={"X-API-Key": "local-test-key"}, json={"question": "Hello"}
        ).status_code == 503


def test_budget_refund_uses_original_day_and_month(monkeypatch):
    fake = fakeredis.FakeRedis(decode_responses=True)
    _reload_app_with_env(monkeypatch, {"MONTHLY_BUDGET_USD": "1"}, fake)
    from app import cost_guard

    dates = iter([datetime(2026, 11, 30, tzinfo=timezone.utc),
                  datetime(2026, 12, 1, tzinfo=timezone.utc)])
    monkeypatch.setattr(cost_guard, "_now_in_budget_tz", lambda: next(dates))
    reservation = cost_guard.reserve_budget(0.00003)
    fake.set("cost:daily:2026-12-01", "0.00005")
    fake.set("cost:monthly:2026-12", "0.00005")
    cost_guard.settle_budget(reservation, 0)

    assert float(fake.get("cost:daily:2026-11-30")) == 0
    assert float(fake.get("cost:monthly:2026-11")) == 0
    assert float(fake.get("cost:daily:2026-12-01")) == 0.00005
    assert float(fake.get("cost:monthly:2026-12")) == 0.00005


def test_budget_accepts_exact_limit_at_decimal_boundary(monkeypatch):
    fake = fakeredis.FakeRedis(decode_responses=True)
    _reload_app_with_env(monkeypatch, {"DAILY_BUDGET_USD": "0.0000336"}, fake)
    from app import cost_guard

    cost_guard.reserve_budget(0.00001425)
    cost_guard.reserve_budget(0.00001935)
    assert cost_guard.get_current_spend()["daily_usd"] == 0.0000336


def test_budget_declines_output_that_would_exceed_monthly_cap(monkeypatch):
    fake = fakeredis.FakeRedis(decode_responses=True)
    app, _, _ = _reload_app_with_env(monkeypatch, {
        "AGENT_API_KEY": "local-test-key", "MONTHLY_BUDGET_USD": "0.000020"
    }, fake)
    monkeypatch.setattr(app, "llm_ask", lambda _: "x" * 200)

    with TestClient(app.app) as client:
        response = client.post(
            "/ask", headers={"X-API-Key": "local-test-key"}, json={"question": "Hi"}
        )
        from app.cost_guard import get_current_spend

        assert response.status_code == 402
        assert get_current_spend() == {"daily_usd": 0, "monthly_usd": 0}


def test_non_ascii_api_key_comparison_preserves_401(monkeypatch):
    from fastapi import HTTPException

    _reload_app_with_env(monkeypatch, {"AGENT_API_KEY": "clé"}, None)
    from app.auth import verify_api_key

    with pytest.raises(HTTPException) as err:
        verify_api_key("wrong")
    assert err.value.status_code == 401
    assert verify_api_key("clé") == "clé"


def test_unexpected_exception_does_not_log_credentials(monkeypatch):
    fake = fakeredis.FakeRedis(decode_responses=True)
    canary = "local-exception-canary"
    app, _, _ = _reload_app_with_env(monkeypatch, {"AGENT_API_KEY": canary}, fake)
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(app.JsonFormatter())
    app.logger.addHandler(handler)

    def fail(_question):
        raise RuntimeError(f"Backend included {canary}")

    monkeypatch.setattr(app, "llm_ask", fail)
    try:
        with TestClient(app.app) as client:
            response = client.post(
                "/ask", headers={"X-API-Key": canary}, json={"question": "Hello"}
            )
        assert response.status_code == 500
        records = [json.loads(line) for line in buffer.getvalue().splitlines()]
        assert any(record["event"] == "unhandled_error" for record in records)
        assert canary not in buffer.getvalue()
    finally:
        app.logger.removeHandler(handler)
