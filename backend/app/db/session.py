"""Database engine and session management.

PostgreSQL is the system of record. This module owns the connection pool
and the session factory; request handlers and Celery tasks obtain a
session through get_db() (or SessionLocal directly in the worker).

Two properties of the engine matter for reliability:

* **Every wait is bounded.** Connecting to PostgreSQL and waiting for a
  free pooled connection both have a timeout. Without them, an
  unreachable database or a saturated pool blocks the request until the
  operating system gives up, and the request looks like a hung
  connection rather than a failure.
* **Sessions are closed safely.** A session is not thread safe, and
  FastAPI runs a request handler in a worker thread while the teardown
  of the same request's dependency may run in another one. When a
  request is cancelled (client disconnect, server shutdown) the teardown
  can reach ``Session.close()`` while the handler is still waiting for a
  connection. SQLAlchemy then refuses the close with
  ``IllegalStateChangeError``, which replaces the real error
  (``CancelledError``) and hides the actual cause. close_session()
  handles that case explicitly.
"""

import logging
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IllegalStateChangeError
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

logger = logging.getLogger("axiom.db")

# Connection arguments handed to the driver. Kept in a module constant so
# the effective values can be inspected and asserted on.
CONNECT_ARGS = {"connect_timeout": settings.db_connect_timeout_seconds}

engine = create_engine(
    settings.database_url,
    # pool_pre_ping issues a cheap "SELECT 1" before handing out a pooled
    # connection. Without it, a connection that the database closed while
    # idle (restart, network blip) surfaces as a confusing error on the
    # next request instead of being transparently replaced.
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    # Bound the wait for a pooled connection. A saturated pool must
    # produce an error, not an indefinite wait.
    pool_timeout=settings.db_pool_timeout_seconds,
    # Discard connections that have been idle for too long. A database or
    # firewall may drop them without the pool noticing, and the next
    # borrower would then use a dead socket.
    pool_recycle=settings.db_pool_recycle_seconds,
    # Bound a single TCP connection attempt. psycopg accepts
    # connect_timeout in seconds; without it an unreachable database
    # blocks until the OS gives up, which can take minutes.
    connect_args=CONNECT_ARGS,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    # Do not expire ORM attributes after commit; the API layer serialises
    # objects right after committing and lazy-loading would need a new
    # session otherwise.
    expire_on_commit=False,
)


def psycopg_dsn(database_url: str | None = None) -> str:
    """Convert a SQLAlchemy URL into a libpq/psycopg connection string.

    settings.database_url is a SQLAlchemy URL such as
    "postgresql+psycopg://user:pass@host:5432/db". The "+psycopg" part is
    a SQLAlchemy dialect selector and is meaningless to psycopg, which
    only understands the plain "postgresql://" scheme. Passing the URL
    straight through raises ProgrammingError, which is how a readiness
    check can report "database down" while the application is talking to
    that very database successfully.
    """
    url = make_url(database_url if database_url is not None else settings.database_url)
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def close_session(db: Session) -> None:
    """Close a session, tolerating a concurrent teardown race.

    ``Session.close()`` is refused with ``IllegalStateChangeError`` when
    another thread is inside the session at the same time - typically
    still waiting for a pooled connection inside ``_connection_for_bind()``.
    That happens when a request is cancelled while it is waiting for the
    database: FastAPI tears the dependency down in one thread while the
    handler is still blocked in another one.

    Raising from the teardown would replace the original
    ``CancelledError`` with a confusing SQLAlchemy error, so the race is
    reported and the original exception is allowed to propagate. The
    connection that was being checked out is not returned to the pool in
    a half-used state: SQLAlchemy discards it when the blocked statement
    finally gives up.
    """
    try:
        db.close()
    except IllegalStateChangeError:
        logger.warning(
            "Session teardown raced with an in-flight statement; the "
            "session was left for the aborted request to release."
        )


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        close_session(db)


def dispose_engine() -> None:
    """Close every pooled connection.

    Called when the application shuts down, so the process does not keep
    PostgreSQL connections open after the server stopped accepting
    requests.
    """
    engine.dispose()
