"""Pytest configuration for Axiom backend tests.

Ensures the backend package is importable regardless of the working
directory pytest is invoked from, and provides shared helpers/fixtures.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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

    from app.db.session import SessionLocal, engine, psycopg_dsn

    try:
        probe = psycopg.connect(psycopg_dsn(), connect_timeout=3)
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


# ---------------------------------------------------------------------------
# Storage adapters
# ---------------------------------------------------------------------------

@pytest.fixture
def local_storage(tmp_path):
    """A LocalFileStorage rooted in a unique temporary directory.

    The root is deliberately not ``backend/storage``: storage tests must
    never create objects in the repository working tree.
    """
    from app.storage.local import LocalFileStorage

    return LocalFileStorage(tmp_path / "storage")


@pytest.fixture
def memory_storage():
    """A fresh in-memory storage adapter for one test."""
    from tests.fakes.storage import InMemoryFileStorage

    return InMemoryFileStorage()


@pytest.fixture(params=["local", "memory"])
def storage_adapter(request, local_storage, memory_storage):
    """Run a test against both implementations of the storage contract."""
    if request.param == "local":
        return local_storage
    if request.param == "memory":
        return memory_storage
    raise AssertionError("Unknown storage adapter %r" % request.param)


@pytest.fixture
def override_storage(storage_adapter):
    """Install a storage adapter in FastAPI's dependency map, then restore.

    The override is isolated per test so route tests can choose either the
    local adapter or the in-memory fake without leaking state.
    """
    from app.main import app
    from app.storage import get_storage

    app.dependency_overrides[get_storage] = lambda: storage_adapter
    try:
        yield storage_adapter
    finally:
        app.dependency_overrides.pop(get_storage, None)
