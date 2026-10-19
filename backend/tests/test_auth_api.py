"""Tests for the login endpoint (Phase 2, step 2.2 / 2.7).

The endpoint is exercised against the real PostgreSQL database through
the shared ``db_session`` fixture, which rolls everything back, so no
account is created and no credential is modified.

These cover the behaviour that the reported 401 was checked against:

* a valid password returns a token;
* a wrong password returns 401;
* an unknown email returns the same 401, so the endpoint cannot be used
  to find out which addresses are registered;
* the email is matched case-insensitively;
* the returned token works on a protected endpoint.
"""

import uuid

import pytest
import redis
from fastapi.testclient import TestClient

from app.auth import rate_limit
from app.auth.service import create_user
from app.config import settings


@pytest.fixture
def rate_limit_clean():
    """Clear the limiter counters so the tests do not block each other."""
    try:
        client = redis.Redis.from_url(
            settings.redis_url, socket_connect_timeout=2, socket_timeout=2
        )
        client.ping()
    except Exception:
        client = None

    rate_limit._client = None
    rate_limit._next_attempt_at = 0.0
    rate_limit._redis_reported_down = False
    yield
    if client is not None:
        for key in client.scan_iter("axiom:ratelimit:login:*"):
            client.delete(key)
    rate_limit._client = None
    rate_limit._next_attempt_at = 0.0
    rate_limit._redis_reported_down = False


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


def _account(db_session, email, password):
    """Create a user with one workspace membership."""
    from app.db.models import Workspace, WorkspaceMembership

    user = create_user(db_session, email, password)
    workspace = Workspace(name="ws-%s" % uuid.uuid4().hex)
    db_session.add(workspace)
    db_session.flush()
    db_session.add(
        WorkspaceMembership(
            user_id=user.id, workspace_id=workspace.id, role="owner"
        )
    )
    db_session.flush()
    return email


def test_a_valid_password_returns_a_token(db_session, rate_limit_clean, client):
    email = _account(db_session, "login-ok-%s@example.com" % uuid.uuid4().hex, "correct-horse-1")

    r = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-1"}
    )

    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    # The token must not leak the password or the hash.
    assert "correct-horse-1" not in r.text


def test_a_wrong_password_returns_401(db_session, rate_limit_clean, client):
    email = _account(db_session, "login-bad-%s@example.com" % uuid.uuid4().hex, "correct-horse-1")

    r = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "wrong-horse-1"}
    )

    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"
    assert r.headers.get("www-authenticate") == "Bearer"


def test_an_unknown_email_returns_the_same_answer(db_session, rate_limit_clean, client):
    """The response must not reveal whether the account exists."""
    known = _account(db_session, "known-%s@example.com" % uuid.uuid4().hex, "correct-horse-1")

    r_known = client.post(
        "/api/v1/auth/login", json={"email": known, "password": "wrong-horse-1"}
    )
    r_unknown = client.post(
        "/api/v1/auth/login",
        json={"email": "unknown-%s@example.com" % uuid.uuid4().hex, "password": "wrong-horse-1"},
    )

    assert r_known.status_code == r_unknown.status_code == 401
    assert r_known.json()["error"]["message"] == r_unknown.json()["error"]["message"]


def test_the_email_is_matched_case_insensitively(db_session, rate_limit_clean, client):
    email = _account(db_session, "login-case-%s@example.com" % uuid.uuid4().hex, "correct-horse-1")

    r = client.post(
        "/api/v1/auth/login",
        json={"email": email.upper(), "password": "correct-horse-1"},
    )

    assert r.status_code == 200, r.json()


def test_the_token_works_on_a_protected_endpoint(db_session, rate_limit_clean, client):
    email = _account(db_session, "login-me-%s@example.com" % uuid.uuid4().hex, "correct-horse-1")

    r = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-1"}
    )
    token = r.json()["access_token"]

    r_me = client.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer %s" % token}
    )

    assert r_me.status_code == 200, r_me.json()
    assert r_me.json()["email"] == email
    assert r_me.json()["role"] == "owner"


def test_a_missing_password_is_a_validation_error(client):
    r = client.post("/api/v1/auth/login", json={"email": "someone@example.com"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_the_stored_hash_is_a_bcrypt_hash(db_session):
    """The account is stored as a bcrypt hash, never as plaintext."""
    from sqlalchemy import select

    from app.db.models import User

    email = "hash-check-%s@example.com" % uuid.uuid4().hex
    create_user(db_session, email, "correct-horse-1")
    row = db_session.execute(select(User).where(User.email == email)).scalar_one()
    assert row.password_hash.startswith("$2b$")
    assert len(row.password_hash) == 60
    assert "correct-horse-1" not in row.password_hash
