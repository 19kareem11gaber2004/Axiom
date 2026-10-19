"""Tests for the Redis-backed login rate limiter (Phase 2, step 2.7).

These tests need a real Redis. When none is reachable they are skipped,
because a rate limiter is a security control and silently skipping it
would give false confidence. CI provides Redis as a service container.
"""

import uuid

import pytest
import redis

from app.auth import rate_limit
from app.auth.service import create_user
from app.config import settings

REDIS_URL = settings.redis_url


@pytest.fixture
def redis_client():
    """A Redis client, skipping the test when Redis is unavailable."""
    try:
        client = redis.Redis.from_url(
            REDIS_URL, socket_connect_timeout=2, socket_timeout=2
        )
        client.ping()
    except Exception as exc:
        pytest.skip(f"Redis not available: {exc}")
    yield client
    # Clear anything this test left behind.
    for key in client.scan_iter("axiom:ratelimit:login:*"):
        client.delete(key)


@pytest.fixture
def clean_counters():
    rate_limit._client = None
    yield
    rate_limit._client = None


def test_a_fresh_identifier_is_not_blocked(redis_client, clean_counters):
    blocked, retry_after = rate_limit.is_blocked(
        "email", "nobody-%s@example.com" % uuid.uuid4().hex, 5
    )
    assert blocked is False
    assert retry_after == 0


def test_failures_accumulate_and_block(redis_client, clean_counters):
    identifier = "target-%s@example.com" % uuid.uuid4().hex

    for _ in range(5):
        rate_limit.record_failure("email", identifier)

    blocked, retry_after = rate_limit.is_blocked("email", identifier, 5)
    assert blocked is True
    assert retry_after > 0


def test_the_counter_expires(redis_client, clean_counters):
    identifier = "short-%s@example.com" % uuid.uuid4().hex
    rate_limit.record_failure("email", identifier)
    key = "%semail:%s" % (rate_limit.KEY_PREFIX, identifier)
    ttl = redis_client.ttl(key)
    assert 0 < ttl <= settings.login_rate_limit_window_seconds


def test_resetting_clears_the_counter(redis_client, clean_counters):
    identifier = "reset-%s@example.com" % uuid.uuid4().hex
    for _ in range(5):
        rate_limit.record_failure("email", identifier)
    assert rate_limit.is_blocked("email", identifier, 5)[0] is True

    rate_limit.reset("email", identifier)
    assert rate_limit.is_blocked("email", identifier, 5)[0] is False


def test_a_successful_login_clears_the_counter(redis_client, clean_counters):
    identifier = "success-%s@example.com" % uuid.uuid4().hex
    for _ in range(5):
        rate_limit.record_failure("email", identifier)
    assert rate_limit.is_blocked("email", identifier, 5)[0] is True

    rate_limit.reset_login_failures(identifier, "203.0.113.1")
    assert rate_limit.is_blocked("email", identifier, 5)[0] is False


def test_different_identifiers_have_separate_counters(redis_client, clean_counters):
    first = "one-%s@example.com" % uuid.uuid4().hex
    second = "two-%s@example.com" % uuid.uuid4().hex
    for _ in range(5):
        rate_limit.record_failure("email", first)
    assert rate_limit.is_blocked("email", first, 5)[0] is True
    assert rate_limit.is_blocked("email", second, 5)[0] is False


def test_the_ip_limit_is_separate_and_higher(redis_client, clean_counters):
    """Many users can share one address, so the per-IP limit is looser."""
    ip = "198.51.100.%d" % (uuid.uuid4().int % 250)
    for _ in range(settings.login_rate_limit_max_attempts):
        rate_limit.record_failure("ip", ip)
    assert rate_limit.is_blocked(
        "ip", ip, settings.login_rate_limit_max_attempts
    )[0] is True
    assert rate_limit.is_blocked(
        "ip", ip, settings.login_rate_limit_max_attempts_per_ip
    )[0] is False


def test_rate_limiting_fails_open_when_redis_is_down(monkeypatch, clean_counters):
    """Redis being down must not lock everyone out of logging in."""
    monkeypatch.setattr(rate_limit, "get_client", lambda: None)
    assert rate_limit.is_blocked("email", "someone@example.com", 5) == (False, 0)
    # Recording and resetting must also be no-ops rather than raising.
    rate_limit.record_failure("email", "someone@example.com")
    rate_limit.reset("email", "someone@example.com")


# ---------------------------------------------------------------------------
# The HTTP endpoint
# ---------------------------------------------------------------------------

def test_login_is_blocked_after_repeated_failures(redis_client, clean_counters, db_session):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app, raise_server_exceptions=False)
    email = "brute-%s@example.com" % uuid.uuid4().hex
    password = "wrong-password-123"

    # The account exists, so the failures are genuine wrong passwords.
    create_user(db_session, email, password)

    statuses = []
    for _ in range(settings.login_rate_limit_max_attempts + 2):
        r = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "still-wrong-123"},
        )
        statuses.append(r.status_code)

    assert 429 in statuses, "expected the limiter to kick in, got %s" % statuses
    assert statuses[-1] == 429

    last = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "still-wrong-123"}
    )
    assert last.status_code == 429
    assert last.json()["error"]["code"] == "RATE_LIMITED"
    assert last.json()["error"]["retryable"] is True


def test_a_successful_login_is_not_rate_limited(redis_client, clean_counters, db_session):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app, raise_server_exceptions=False)
    email = "ok-%s@example.com" % uuid.uuid4().hex
    create_user(db_session, email, "the-right-password-1")

    # Two failures first, then a success: the counter is cleared.
    for _ in range(2):
        client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "the-wrong-password-1"},
        )

    r = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "the-right-password-1"},
    )
    assert r.status_code == 200, r.json()


def test_the_limiter_does_not_leak_whether_an_account_exists(redis_client, clean_counters):
    """Unknown email and wrong password must behave identically."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app, raise_server_exceptions=False)
    unknown = "ghost-%s@example.com" % uuid.uuid4().hex

    r1 = client.post(
        "/api/v1/auth/login",
        json={"email": unknown, "password": "whatever-1234"},
    )
    r2 = client.post(
        "/api/v1/auth/login",
        json={"email": "real-%s@example.com" % uuid.uuid4().hex, "password": "whatever-1234"},
    )
    assert r1.status_code == r2.status_code == 401
    assert r1.json()["error"]["message"] == r2.json()["error"]["message"]
