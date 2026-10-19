"""Pytest configuration for Axiom backend tests.

Ensures the backend package is importable regardless of the working
directory pytest is invoked from, and provides shared helpers/fixtures.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def psycopg_dsn(database_url: str) -> str:
    """Convert a SQLAlchemy URL into a libpq/psycopg connection string.

    settings.database_url is a SQLAlchemy URL such as
    "postgresql+psycopg://user:pass@host:5432/db". psycopg.connect() only
    understands the plain "postgresql://" scheme, so passing the URL
    straight through raises ProgrammingError.
    """
    from sqlalchemy.engine import make_url

    url = make_url(database_url)
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture
def db_session():
    """Yield a SQLAlchemy session bound to a real PostgreSQL database.

    The session joins an outer connection-level transaction using
    join_transaction_mode="create_savepoint". That matters because the
    service layer calls session.commit(): with the default "conditional
    savepoint" mode a commit would release the savepoint but ALSO commit
    the outer transaction, permanently persisting test rows. Here the
    savepoint is released and the outer transaction is rolled back at the
    end of the test, so every test starts clean and leaves nothing behind.

    Skips the test when no database is reachable.
    """
    import psycopg

    from app.config import settings
    from app.db.session import SessionLocal, engine

    try:
        probe = psycopg.connect(
            psycopg_dsn(settings.database_url), connect_timeout=3
        )
        probe.close()
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")

    connection = engine.connect()
    outer_transaction = connection.begin()
    session = SessionLocal(
        bind=connection,
        join_transaction_mode="create_savepoint",
    )

    # Make the API routes use THIS session instead of opening their own.
    # Without this the route would query a different connection and could
    # not see the rows the test created (they are still uncommitted), so
    # every request would look like "no such user".
    from app.db.session import get_db
    from app.main import app

    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield session
    finally:
        app.dependency_overrides.pop(get_db, None)
        session.close()
        outer_transaction.rollback()
        connection.close()
