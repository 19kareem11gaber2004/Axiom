"""Axiom API entrypoint.

Provides health and readiness endpoints. Readiness performs bounded
connectivity checks against PostgreSQL and Redis so requests never hang
when a dependency is temporarily unavailable.

The lifespan handler reports the effective configuration at startup and
releases pooled connections at shutdown, so a stopped process does not
keep database or Redis connections open.
"""

import logging
import re
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.errors import ErrorBoundaryMiddleware, register_error_handlers
from app.api.routes import auth as auth_routes
from app.api.routes import documents as document_routes
from app.api.routes import workspaces as workspace_routes
from app.auth import rate_limit
from app.config import settings

logger = logging.getLogger("axiom.main")


def _database_target() -> str:
    """The database host:port/database, without the credentials."""
    try:
        from sqlalchemy.engine import make_url

        url = make_url(settings.database_url)
        return "%s:%s/%s" % (url.host, url.port or 5432, url.database)
    except Exception:  # noqa: BLE001 - display only
        return "<unparseable DATABASE_URL>"


def _redis_target() -> str:
    """The Redis host:port, without any credentials."""
    try:
        from urllib.parse import urlparse

        parsed = urlparse(settings.redis_url)
        return "%s:%s" % (parsed.hostname, parsed.port or 6379)
    except Exception:  # noqa: BLE001 - display only
        return "<unparseable REDIS_URL>"


def configure_logging() -> None:
    """Send the application's own log records to stderr.

    Uvicorn configures its own loggers but leaves the root logger
    alone, so without this the application's messages would only reach
    the terminal through logging's "last resort" handler: warnings, in a
    bare line, and nothing at all at INFO level. The startup summary in
    particular has to be visible - it is how an operator sees which
    database, which Redis and which rate-limit posture the process
    actually uses.
    """
    app_logger = logging.getLogger("axiom")
    if not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        app_logger.addHandler(handler)
    app_logger.setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Report the effective configuration and clean up on shutdown.

    The configuration is logged without secrets: only the hosts the
    process talks to, the environment name, and the rate-limit failure
    mode. The JWT secret and the database password are never logged.
    """
    configure_logging()
    logger.info(
        "Starting Axiom API in the %s environment. "
        "PostgreSQL=%s Redis=%s rate-limit-failure-mode=%s",
        settings.env,
        _database_target(),
        _redis_target(),
        settings.login_rate_limit_failure_mode,
    )
    try:
        yield
    finally:
        # Release the pooled PostgreSQL connections and the cached Redis
        # connection instead of leaving them open until the process dies.
        try:
            from app.db.session import dispose_engine

            dispose_engine()
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            logger.warning("Could not dispose the database engine: %s", exc)
        try:
            rate_limit.close_client()
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            logger.warning("Could not close the Redis client: %s", exc)
        logger.info("Axiom API stopped.")


app = FastAPI(title="Axiom", version="0.1.0", lifespan=lifespan)
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
    client = None
    try:
        import redis

        client = redis.Redis.from_url(
            settings.redis_url, socket_connect_timeout=3, socket_timeout=3
        )
        return bool(client.ping())
    except Exception:
        return False
    finally:
        if client is not None:
            # The probe owns its client; release the connection instead
            # of leaving it to the garbage collector.
            try:
                client.close()
            except Exception:  # noqa: BLE001 - best effort cleanup
                pass


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
