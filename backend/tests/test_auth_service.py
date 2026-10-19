"""Tests for the authentication service (Step 2.2).

Covers password hashing, JWT access tokens, and credential checking.

The password and token tests need no database and always run. The user
tests need a real PostgreSQL and use the shared db_session fixture; CI
provides one through its service container.
"""

import uuid
from datetime import timedelta

import jwt
import pytest

from app.auth.service import (
    MAX_PASSWORD_BYTES,
    TokenError,
    UserAlreadyExistsError,
    authenticate_user,
    create_access_token,
    create_user,
    decode_access_token,
    hash_password,
    verify_password,
)
from app.config import settings


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def test_hash_password_produces_a_bcrypt_hash():
    hashed = hash_password("correct horse battery staple")
    assert hashed.startswith("$2b$")
    assert len(hashed) == 60


def test_hash_password_is_salted_so_hashes_differ():
    """Two hashes of the same password must differ (unique random salt)."""
    first = hash_password("same-password")
    second = hash_password("same-password")
    assert first != second


def test_hash_password_does_not_leak_the_plaintext():
    assert "correct horse battery staple" not in hash_password("correct horse battery staple")


def test_verify_password_accepts_the_correct_password():
    hashed = hash_password("s3cret-password")
    assert verify_password("s3cret-password", hashed) is True


def test_verify_password_rejects_the_wrong_password():
    hashed = hash_password("s3cret-password")
    assert verify_password("wrong-password", hashed) is False


def test_verify_password_rejects_a_malformed_hash():
    assert verify_password("anything", "not-a-bcrypt-hash") is False


def test_hash_password_rejects_passwords_over_the_bcrypt_limit():
    """bcrypt ignores bytes past 72, so we refuse rather than truncate."""
    too_long = "a" * (MAX_PASSWORD_BYTES + 1)
    with pytest.raises(ValueError):
        hash_password(too_long)


def test_verify_password_returns_false_for_over_long_candidates():
    hashed = hash_password("short")
    assert verify_password("a" * (MAX_PASSWORD_BYTES + 1), hashed) is False


# ---------------------------------------------------------------------------
# Access tokens
# ---------------------------------------------------------------------------

def test_create_and_decode_access_token_round_trips():
    user_id = uuid.uuid4()
    token = create_access_token(user_id)
    payload = decode_access_token(token)
    assert payload.sub == user_id
    assert payload.type == "access"
    assert payload.exp > payload.iat


def test_access_token_carries_no_workspace_or_role():
    """Workspace and role are resolved server-side, never trusted from a token."""
    token = create_access_token(uuid.uuid4())
    claims = jwt.decode(
        token,
        settings.jwt_secret_key,
        algorithms=[settings.jwt_algorithm],
    )
    assert "workspace_id" not in claims
    assert "role" not in claims


def test_decode_rejects_a_token_signed_with_another_key():
    # HMAC-SHA256 keys should be at least 32 bytes (RFC 7518 section 3.2).
    forged = jwt.encode(
        {"sub": str(uuid.uuid4()), "iat": 0, "exp": 9999999999, "type": "access"},
        "a-different-signing-key-that-is-long-enough!!",
        algorithm="HS256",
    )
    with pytest.raises(TokenError):
        decode_access_token(forged)


def test_decode_rejects_a_token_with_a_tampered_payload():
    """Flipping a character in the payload must invalidate the signature."""
    token = create_access_token(uuid.uuid4())
    head, payload, signature = token.split(".")
    tampered = "%s%s.%s" % (head, payload[:-2] + ("aa" if not payload.endswith("aa") else "bb"), signature)
    with pytest.raises(TokenError):
        decode_access_token(tampered)


def test_decode_rejects_an_alg_none_token():
    """Algorithm confusion: a token claiming alg=none must never be accepted."""
    unsigned = jwt.encode(
        {"sub": str(uuid.uuid4()), "iat": 0, "exp": 9999999999, "type": "access"},
        key="",
        algorithm="none",
    )
    with pytest.raises(TokenError):
        decode_access_token(unsigned)


def test_decode_rejects_an_expired_token():
    expired = create_access_token(uuid.uuid4(), expires_delta=timedelta(seconds=-10))
    with pytest.raises(TokenError):
        decode_access_token(expired)


def test_decode_rejects_a_token_without_an_expiry():
    """A token that never expires would be a permanent credential."""
    no_expiry = jwt.encode(
        {"sub": str(uuid.uuid4()), "iat": 0, "type": "access"},
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(TokenError):
        decode_access_token(no_expiry)


def test_decode_rejects_a_non_access_token_type():
    other = jwt.encode(
        {"sub": str(uuid.uuid4()), "iat": 0, "exp": 9999999999, "type": "refresh"},
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(TokenError):
        decode_access_token(other)


def test_decode_rejects_garbage():
    with pytest.raises(TokenError):
        decode_access_token("not-a-token")


# ---------------------------------------------------------------------------
# Users and credentials (requires PostgreSQL)
# ---------------------------------------------------------------------------

def test_create_user_stores_a_hash_not_the_password(db_session):
    email = "user-%s@example.com" % uuid.uuid4().hex
    user = create_user(db_session, email, "my-password-123")
    assert user.id is not None
    assert user.email == email
    assert user.password_hash != "my-password-123"
    assert user.password_hash.startswith("$2b$")


def test_create_user_normalises_the_email(db_session):
    email = "Mixed-%s@Example.COM" % uuid.uuid4().hex
    user = create_user(db_session, email, "my-password-123")
    assert user.email == email.lower()


def test_create_user_rejects_a_duplicate_email(db_session):
    email = "dup-%s@example.com" % uuid.uuid4().hex
    create_user(db_session, email, "my-password-123")
    with pytest.raises(UserAlreadyExistsError):
        create_user(db_session, email, "another-password-456")


def test_authenticate_user_returns_the_user_for_valid_credentials(db_session):
    email = "ok-%s@example.com" % uuid.uuid4().hex
    created = create_user(db_session, email, "my-password-123")
    found = authenticate_user(db_session, email, "my-password-123")
    assert found is not None
    assert found.id == created.id


def test_authenticate_user_is_case_insensitive_for_the_email(db_session):
    email = "case-%s@example.com" % uuid.uuid4().hex
    create_user(db_session, email, "my-password-123")
    assert authenticate_user(db_session, email.upper(), "my-password-123") is not None


def test_authenticate_user_returns_none_for_a_wrong_password(db_session):
    email = "wrong-%s@example.com" % uuid.uuid4().hex
    create_user(db_session, email, "my-password-123")
    assert authenticate_user(db_session, email, "not-my-password") is None


def test_authenticate_user_returns_none_for_an_unknown_email(db_session):
    """Unknown email and wrong password must be indistinguishable."""
    assert authenticate_user(db_session, "nobody-%s@example.com" % uuid.uuid4().hex, "x") is None
