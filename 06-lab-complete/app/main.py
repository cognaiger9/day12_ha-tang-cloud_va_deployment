"""
Production AI Agent — Kết hợp tất cả Day 12 concepts

Checklist:
  ✅ Config từ environment (12-factor)
  ✅ Structured JSON logging
  ✅ API Key authentication
  ✅ Rate limiting (Redis sliding window, per-user)
  ✅ Cost guard (Redis monthly budget, per-user)
  ✅ Conversation history (Redis, per-user)
  ✅ Input validation (Pydantic)
  ✅ Health check + Readiness probe (Redis ping)
  ✅ Graceful shutdown (SIGTERM)
  ✅ Security headers
  ✅ CORS
  ✅ Stateless design (all state in Redis)
"""
import os
import time
import signal
import logging
import json
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Security, Depends, Request, Response
from fastapi.security.api_key import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import uvicorn

from app.config import settings
from utils.llm import ask as llm_ask

# ─────────────────────────────────────────────────────────
# Logging — JSON structured
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format='{"ts":"%(asctime)s","lvl":"%(levelname)s","msg":"%(message)s"}',
)
logger = logging.getLogger(__name__)

START_TIME = time.time()
_is_ready = False
_request_count = 0
_error_count = 0

# ─────────────────────────────────────────────────────────
# Redis helpers (async)
# ─────────────────────────────────────────────────────────
_redis = None  # set in lifespan


async def get_redis():
    return _redis


async def redis_rate_limit(user_id: str):
    """Sliding window rate limit using Redis sorted set."""
    if _redis is None:
        return  # no Redis → skip (graceful degradation)
    now = time.time()
    window_start = now - 60
    key = f"ratelimit:{user_id}"
    pipe = _redis.pipeline()
    pipe.zadd(key, {str(now): now})
    pipe.zremrangebyscore(key, 0, window_start)
    pipe.zcard(key)
    pipe.expire(key, 70)
    results = await pipe.execute()
    count = results[2]
    if count > settings.rate_limit_per_minute:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {settings.rate_limit_per_minute} req/min",
            headers={"Retry-After": "60"},
        )


async def redis_check_budget(user_id: str, cost: float):
    """Per-user monthly budget guard using Redis."""
    if _redis is None:
        return
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    key = f"cost:{user_id}:{month}"
    current = float(await _redis.get(key) or 0.0)
    if current + cost > settings.monthly_budget_usd:
        raise HTTPException(
            status_code=402,
            detail=f"Monthly budget of ${settings.monthly_budget_usd} exceeded",
        )
    await _redis.incrbyfloat(key, cost)
    await _redis.expire(key, 2_592_000)  # 30 days


async def get_history(user_id: str) -> list[dict]:
    """Retrieve conversation history from Redis."""
    if _redis is None:
        return []
    raw = await _redis.lrange(f"history:{user_id}", 0, -1)
    history = []
    for item in raw:
        try:
            history.append(json.loads(item))
        except Exception:
            pass
    return history


async def save_history(user_id: str, question: str, answer: str):
    """Append user + assistant turns and trim to max length."""
    if _redis is None:
        return
    key = f"history:{user_id}"
    pipe = _redis.pipeline()
    pipe.rpush(key, json.dumps({"role": "user", "content": question}))
    pipe.rpush(key, json.dumps({"role": "assistant", "content": answer}))
    pipe.ltrim(key, -settings.max_history_length, -1)
    pipe.expire(key, 86400)  # 24h TTL
    await pipe.execute()


# ─────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def verify_api_key(api_key: str = Security(api_key_header)) -> str:
    if not api_key or api_key != settings.agent_api_key:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key. Include header: X-API-Key: <key>",
        )
    return api_key


# ─────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _redis, _is_ready

    logger.info(json.dumps({
        "event": "startup",
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
    }))

    if settings.redis_url:
        try:
            import redis.asyncio as aioredis
            _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
            await _redis.ping()
            logger.info(json.dumps({"event": "redis_connected", "url": settings.redis_url}))
        except Exception as e:
            logger.warning(json.dumps({"event": "redis_unavailable", "error": str(e)}))
            _redis = None
    else:
        logger.warning(json.dumps({"event": "redis_not_configured"}))

    _is_ready = True
    logger.info(json.dumps({"event": "ready"}))

    yield

    _is_ready = False
    if _redis:
        await _redis.aclose()
    logger.info(json.dumps({"event": "shutdown"}))


# ─────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)


@app.middleware("http")
async def request_middleware(request: Request, call_next):
    global _request_count, _error_count
    start = time.time()
    _request_count += 1
    try:
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        if "server" in response.headers:
            del response.headers["server"]
        duration = round((time.time() - start) * 1000, 1)
        logger.info(json.dumps({
            "event": "request",
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "ms": duration,
        }))
        return response
    except Exception:
        _error_count += 1
        raise


# ─────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────
class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    user_id: str = Field(default="default_user", max_length=64)


class AskResponse(BaseModel):
    question: str
    answer: str
    model: str
    user_id: str
    conversation_turn: int
    timestamp: str


# ─────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────
@app.get("/", tags=["Info"])
def root():
    return {
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "endpoints": {
            "ask": "POST /ask (requires X-API-Key)",
            "health": "GET /health",
            "ready": "GET /ready",
        },
    }


@app.post("/ask", response_model=AskResponse, tags=["Agent"])
async def ask_agent(
    body: AskRequest,
    request: Request,
    _key: str = Depends(verify_api_key),
):
    """
    Send a question to the AI agent. Conversation history is retained per user_id.

    **Authentication:** Include header `X-API-Key: <your-key>`
    """
    await redis_rate_limit(body.user_id)

    # Estimate input cost before calling LLM
    input_tokens = len(body.question.split()) * 2
    estimated_cost = (input_tokens / 1000) * 0.00015
    await redis_check_budget(body.user_id, estimated_cost)

    logger.info(json.dumps({
        "event": "agent_call",
        "user_id": body.user_id,
        "q_len": len(body.question),
        "client": str(request.client.host) if request.client else "unknown",
    }))

    history = await get_history(body.user_id)
    answer = llm_ask(body.question, history=history)

    # Record output cost
    output_tokens = len(answer.split()) * 2
    output_cost = (output_tokens / 1000) * 0.0006
    await redis_check_budget(body.user_id, output_cost)

    await save_history(body.user_id, body.question, answer)

    # conversation_turn = number of user messages in history after saving
    turn = (len(history) // 2) + 1

    return AskResponse(
        question=body.question,
        answer=answer,
        model=settings.llm_model,
        user_id=body.user_id,
        conversation_turn=turn,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@app.get("/history/{user_id}", tags=["Agent"])
async def get_conversation_history(
    user_id: str,
    _key: str = Depends(verify_api_key),
):
    """Return the conversation history for a user (requires X-API-Key)."""
    history = await get_history(user_id)
    return {"user_id": user_id, "turns": len(history) // 2, "history": history}


@app.delete("/history/{user_id}", tags=["Agent"])
async def clear_conversation_history(
    user_id: str,
    _key: str = Depends(verify_api_key),
):
    """Clear conversation history for a user."""
    if _redis:
        await _redis.delete(f"history:{user_id}")
    return {"user_id": user_id, "cleared": True}


@app.get("/health", tags=["Operations"])
def health():
    """Liveness probe. Platform restarts container if this fails."""
    return {
        "status": "ok",
        "version": settings.app_version,
        "environment": settings.environment,
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "total_requests": _request_count,
        "llm": "openai" if settings.openai_api_key else "mock",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/ready", tags=["Operations"])
async def ready():
    """Readiness probe. Load balancer stops routing here if not ready."""
    if not _is_ready:
        raise HTTPException(503, "Not ready")
    if _redis:
        try:
            await _redis.ping()
        except Exception as e:
            raise HTTPException(503, f"Redis unavailable: {e}")
    return {"ready": True, "redis": _redis is not None}


@app.get("/metrics", tags=["Operations"])
async def metrics(_key: str = Depends(verify_api_key)):
    """Basic metrics (protected)."""
    return {
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "total_requests": _request_count,
        "error_count": _error_count,
        "redis_connected": _redis is not None,
        "monthly_budget_usd": settings.monthly_budget_usd,
        "rate_limit_per_minute": settings.rate_limit_per_minute,
    }


# ─────────────────────────────────────────────────────────
# Graceful Shutdown
# ─────────────────────────────────────────────────────────
def _handle_signal(signum, _frame):
    logger.info(json.dumps({"event": "signal", "signum": signum}))


signal.signal(signal.SIGTERM, _handle_signal)


if __name__ == "__main__":
    logger.info(f"Starting {settings.app_name} on {settings.host}:{settings.port}")
    logger.info(f"API Key: {settings.agent_api_key[:4]}****")
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        timeout_graceful_shutdown=30,
    )
