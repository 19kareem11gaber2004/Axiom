"""Bounded integration smoke test.

Requires real PostgreSQL and Redis (started via docker compose). Each
connection attempt uses a short timeout; if the services are unavailable
the test is skipped rather than failing, so the suite can run offline.

This test is NOT part of the offline CI gate; CI runs mocked readiness
tests only. Integration smoke is executed manually after compose starts.
"""

import os
import time

import psycopg
import pytest
import redis

from app.config import settings

PG_TIMEOUT = 3
REDIS_TIMEOUT = 3


@pytest.fixture
def pg_conn():
    """Yield a psycopg connection to PostgreSQL, skipping if unavailable."""
    try:
        conn = psycopg.connect(settings.database_url, connect_timeout=PG_TIMEOUT)
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
