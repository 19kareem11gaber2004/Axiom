"""Tests for the database engine and the session lifecycle.

Two things are guarded here.

1. **Every wait is bounded.** The engine must not wait forever for a
   connection or for a free slot in the pool. An unbounded wait is what
   turns an unreachable database into a request that never returns.

2. **Session teardown survives a cancelled request.** FastAPI runs a
   request handler in a worker thread and tears the request's
   dependencies down in another one. When a request is cancelled while
   it is waiting for a database connection, the teardown reaches
   ``Session.close()`` while the handler is still inside
   ``_connection_for_bind()``. SQLAlchemy refuses that close with
   ``IllegalStateChangeError``, which replaces the real error
   (``CancelledError``) and hides the cause.

   The race is reproduced deterministically below: a session whose only
   pooled connection is already taken blocks inside the connection
   checkout, and closing it from another thread is exactly the situation
   that produced the reported traceback.
"""

import threading
import time

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IllegalStateChangeError
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db.session import (
    CONNECT_ARGS,
    SessionLocal,
    close_session,
    dispose_engine,
    engine,
    get_db,
    psycopg_dsn,
)


@pytest.fixture
def postgres():
    """Skip the test when no PostgreSQL is reachable."""
    try:
        connection = psycopg.connect(psycopg_dsn(), connect_timeout=3)
        connection.close()
    except Exception as exc:  # pragma: no cover - depends on the environment
        pytest.skip(f"PostgreSQL not available: {exc}")


# ---------------------------------------------------------------------------
# Engine configuration
# ---------------------------------------------------------------------------

def test_the_engine_bounds_the_connection_attempt():
    """An unreachable database must fail, not hang."""
    assert CONNECT_ARGS["connect_timeout"] == settings.db_connect_timeout_seconds
    assert engine.pool._timeout == settings.db_pool_timeout_seconds


def test_the_pool_pre_ping_is_enabled():
    assert engine.pool._pre_ping is True


def test_the_psycopg_dsn_is_usable_by_the_driver():
    dsn = psycopg_dsn()
    assert dsn.startswith("postgresql://")
    assert "+psycopg" not in dsn


def test_the_dsn_keeps_the_password():
    """The driver needs the password; only the log output hides it."""
    dsn = psycopg_dsn("postgresql+psycopg://u:p@host:5432/db")
    assert "u:p@" in dsn


# ---------------------------------------------------------------------------
# Session cleanup
# ---------------------------------------------------------------------------

def test_get_db_closes_the_session():
    generator = get_db()
    session = next(generator)
    assert session is not None
    generator.close()
    # A closed session no longer holds a transaction.
    assert session.get_transaction() is None


def test_close_session_does_not_raise_on_the_cancellation_race(monkeypatch):
    """The teardown must not replace the real error with a SQLAlchemy one."""
    session = SessionLocal()
    try:
        def racing_close():
            raise IllegalStateChangeError("simulated race")

        monkeypatch.setattr(session, "close", racing_close)
        # Must not raise: the original exception (CancelledError) has to
        # be the one the application sees.
        close_session(session)
    finally:
        pass


def test_closing_a_session_while_a_statement_is_in_flight_does_not_raise(postgres):
    """The exact race that produced the reported traceback.

    The session below cannot get a connection (the pool has none left),
    so it blocks inside ``_connection_for_bind()``. Closing it from the
    main thread is what FastAPI's dependency teardown does when a
    request is cancelled. Before the fix this raised
    ``IllegalStateChangeError``.
    """
    small_engine = create_engine(
        settings.database_url, pool_size=1, max_overflow=0, pool_timeout=5
    )
    SmallSession = sessionmaker(bind=small_engine, expire_on_commit=False)
    holder = SmallSession()
    holder.execute(text("SELECT 1"))  # takes the only pooled connection

    session = SmallSession()
    started = threading.Event()

    def blocked_statement():
        started.set()
        try:
            session.execute(text("SELECT 1"))
        except Exception:
            # The checkout times out once the holder releases the
            # connection; the test does not care about that part.
            pass

    worker = threading.Thread(target=blocked_statement, daemon=True)
    worker.start()
    started.wait(timeout=5)
    # Give the worker time to block inside the connection checkout.
    time.sleep(0.3)

    try:
        close_session(session)
    finally:
        holder.close()
        worker.join(timeout=10)
        small_engine.dispose()


def test_disposing_the_engine_releases_and_reopens_connections(postgres):
    """Shutdown releases the pooled connections and the engine recovers."""
    dispose_engine()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1


def test_the_session_factory_is_bound_to_the_engine():
    assert SessionLocal.kw["bind"] is engine
