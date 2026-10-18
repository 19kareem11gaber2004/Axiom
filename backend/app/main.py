"""Axiom API entrypoint.

Provides health and readiness endpoints. Readiness performs bounded
connectivity checks against PostgreSQL and Redis so requests never hang
when a dependency is temporarily unavailable.
"""

import re
import uuid
from typing import Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.config import settings

app = FastAPI(title="Axiom", version="0.1.0")

# Request ID must be 1–64 characters and contain only safe header characters.
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _generate_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:16]}"


def _validate_request_id(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if REQUEST_ID_RE.match(value):
        return value
    return None


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    client_id = request.headers.get("x-request-id")
    request_id = _validate_request_id(client_id) or _generate_request_id()
    response: Response = await call_next(request)
    response.headers["x-request-id"] = request_id
    return response


def check_postgres() -> bool:
    """Return True if PostgreSQL accepts a trivial query within timeout."""
    try:
        from psycopg import connect

        conn = connect(settings.database_url, connect_timeout=3)
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        conn.close()
        return True
    except Exception:
        return False


def check_redis() -> bool:
    """Return True if Redis responds to PING within timeout."""
    try:
        import redis

        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=3, socket_timeout=3)
        return bool(client.ping())
    except Exception:
        return False


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def ready():
    pg_ok = check_postgres()
    redis_ok = check_redis()
    checks = {
        "postgres": "up" if pg_ok else "down",
        "redis": "up" if redis_ok else "down",
    }
    if pg_ok and redis_ok:
        return {"status": "ready", "checks": checks}
    return JSONResponse(status_code=503, content={"status": "not_ready", "checks": checks})
