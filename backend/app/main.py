"""Axiom API entrypoint.

Provides health and readiness endpoints. Readiness performs bounded
connectivity checks against PostgreSQL and Redis so requests never hang
when a dependency is temporarily unavailable.
"""

import re
import uuid
from typing import Optional

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.errors import ErrorBoundaryMiddleware, register_error_handlers
from app.api.routes import auth as auth_routes
from app.api.routes import documents as document_routes
from app.api.routes import workspaces as workspace_routes
from app.config import settings

app = FastAPI(title="Axiom", version="0.1.0")
register_error_handlers(app)

# Middleware order matters. Starlette builds the stack so that the LAST
# middleware added is the OUTERMOST one. We want, from outside in:
#
#   request_id  ->  CORS  ->  error boundary  ->  router
#
# The error boundary must sit inside CORS so a 500 response still gets
# CORS headers; the browser would otherwise report a CORS error instead
# of a server error. The request ID must be outermost so every response,
# including errors, carries it.
app.add_middleware(ErrorBoundaryMiddleware)

# CORS is a browser-enforced policy, not access control: it decides
# whether JavaScript may read a response, and never decides who the caller
# is. Authentication and authorization are handled by the auth module.
#
# The header list must include every custom header the frontend sends,
# or the browser's preflight fails and the real request is never made.
# "X-Workspace-ID" is the one that is easy to forget.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Request-ID",
        "X-Workspace-ID",
    ],
    # Let the browser read the request ID so it can be shown in support
    # requests and correlated with server logs.
    expose_headers=["X-Request-ID"],
    max_age=600,
)

app.include_router(auth_routes.router)
app.include_router(workspace_routes.router)
app.include_router(document_routes.router)

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
    # Store on request.state so error handlers and route handlers can echo
    # the same ID back to the client and include it in logs.
    request.state.request_id = request_id
    response: Response = await call_next(request)
    response.headers["x-request-id"] = request_id
    return response


def check_postgres() -> bool:
    """Return True if PostgreSQL accepts a trivial query within timeout."""
    try:
        from psycopg import connect

        from app.db.session import psycopg_dsn

        conn = connect(psycopg_dsn(), connect_timeout=3)
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
