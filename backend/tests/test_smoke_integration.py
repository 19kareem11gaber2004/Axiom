"""Bounded integration smoke test.

Requires real PostgreSQL and Redis (started via docker compose, or any
reachable instance). Each connection attempt uses a short timeout; if the
services are unavailable the test is skipped rather than failing, so the
suite can run offline.

This test is NOT part of the offline CI gate; CI runs mocked readiness
tests only. Integration smoke is executed manually after compose starts.
"""

import psycopg
import pytest
import redis

from app.config import settings
from app.db.session import psycopg_dsn

PG_TIMEOUT = 3
REDIS_TIMEOUT = 3


@pytest.fixture
def pg_conn():
    """Yield a psycopg connection to PostgreSQL, skipping if unavailable."""
    try:
        conn = psycopg.connect(psycopg_dsn(), connect_timeout=PG_TIMEOUT)
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
    yield conn
    conn.close()


@pytest.fixture
def redis_client():
    """Yield a Redis client, skipping if unavailable."""
    try:
        client = redis.Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=REDIS_TIMEOUT,
            socket_timeout=REDIS_TIMEOUT,
        )
        client.ping()
    except Exception as exc:
        pytest.skip(f"Redis not available: {exc}")
    yield client


def test_postgres_accepts_select_1(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)


def test_redis_pings(redis_client):
    assert redis_client.ping() is True


def test_database_tables_exist(pg_conn):
    """Smoke-check that the v0.1 schema has been applied."""
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name IN (
                'users', 'workspaces', 'workspace_memberships',
                'documents', 'processing_runs', 'chunks',
                'conversations', 'messages', 'citations'
              )
            """
        )
        count = cur.fetchone()[0]
    assert count == 9, f"expected 9 v0.1 tables, found {count}"


def test_alembic_revision_is_applied(pg_conn):
    """The database must be migrated to the latest revision."""
    with pg_conn.cursor() as cur:
        cur.execute("SELECT version_num FROM alembic_version")
        row = cur.fetchone()
    assert row is not None, "alembic_version is empty - run 'alembic upgrade head'"
    assert row[0] == "0002", f"expected revision 0002, found {row[0]}"


def test_enum_types_exist_once(pg_conn):
    """Each shared ENUM type must exist exactly once with the expected values."""
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.typname,
                   string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder)
            FROM pg_type t
            JOIN pg_enum e ON e.enumtypid = t.oid
            JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE n.nspname = 'public' AND t.typtype = 'e'
            GROUP BY t.typname
            """
        )
        enums = dict(cur.fetchall())

    assert enums == {
        "document_status": "uploaded,queued,processing,retrying,indexed,failed",
        "membership_role": "owner,admin,member,viewer",
        "message_role": "user,assistant",
    }


def test_pgvector_extension_and_column(pg_conn):
    """pgvector must be installed and chunks.embedding must be vector(768)."""
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")
        assert cur.fetchone()[0] == 1, "vector extension is not installed"

        cur.execute(
            """
            SELECT format_type(a.atttypid, a.atttypmod)
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname = 'chunks'
              AND a.attname = 'embedding'
            """
        )
        row = cur.fetchone()
    assert row is not None, "chunks.embedding is missing"
    assert row[0] == "vector(768)", f"expected vector(768), found {row[0]}"
