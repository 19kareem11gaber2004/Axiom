"""Tests for authentication and server-side authorization (Step 2.3).

These exercise the real HTTP surface: a login produces a token, and the
protected endpoints decide what that token may do by reading the
database - never by trusting the token or the request.

The database-backed tests use the shared db_session fixture, so CI
provides PostgreSQL through its service container.
"""

import uuid

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.errors import register_error_handlers
from app.auth.dependencies import ROLE_RANK, WORKSPACE_HEADER
from app.auth.service import create_access_token
from app.db.models import User, Workspace, WorkspaceMembership
from app.main import app

client = TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def account(db_session):
    """Create a user with one workspace membership and return both ids.

    The membership row is written inside the test transaction, so it is
    rolled back with everything else.
    """
    db = db_session
    user = User(
        email="auth-%s@example.com" % uuid.uuid4().hex,
        password_hash="$2b$12$APB7JBc6bwnP9us0otNUAe4MlRYIcY0azrFhCdpCYuxWm.CxSjaI.",
    )
    workspace = Workspace(name="ws-%s" % uuid.uuid4().hex)
    db.add_all([user, workspace])
    db.flush()

    db.add(
        WorkspaceMembership(
            user_id=user.id,
            workspace_id=workspace.id,
            role="member",
        )
    )
    db.flush()
    return {
        "session": db,
        "user_id": user.id,
        "email": user.email,
        "workspace_id": workspace.id,
        "role": "member",
    }


def _auth_header(user_id) -> dict:
    return {"Authorization": "Bearer %s" % create_access_token(user_id)}


# ---------------------------------------------------------------------------
# Role hierarchy
# ---------------------------------------------------------------------------

def test_owner_and_admin_share_the_same_rank():
    """v0.1 treats owner and admin as equivalent (03-erd-api.md section 5)."""
    assert ROLE_RANK["owner"] == ROLE_RANK["admin"]


def test_require_role_rejects_an_unknown_role():
    from app.auth.dependencies import require_role

    with pytest.raises(ValueError):
        require_role("superuser")


def test_require_role_needs_at_least_one_role():
    from app.auth.dependencies import require_role

    with pytest.raises(ValueError):
        require_role()


# ---------------------------------------------------------------------------
# get_current_user
# ---------------------------------------------------------------------------

def test_me_requires_authentication(account):
    r = client.get("/api/v1/auth/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"


def test_me_rejects_a_missing_bearer_token(account):
    r = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer"})
    assert r.status_code == 401


def test_me_rejects_a_non_bearer_scheme(account):
    r = client.get("/api/v1/auth/me", headers={"Authorization": "Basic abc123"})
    assert r.status_code == 401


def test_me_rejects_a_garbage_token(account):
    r = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-token"})
    assert r.status_code == 401


def test_me_rejects_a_token_for_a_deleted_account(account):
    """A valid signature is not enough: the account must still exist."""
    token = create_access_token(uuid.uuid4())
    r = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer %s" % token})
    assert r.status_code == 401


def test_me_resolves_the_principal_from_the_database(account):
    r = client.get("/api/v1/auth/me", headers=_auth_header(account["user_id"]))
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == str(account["user_id"])
    assert body["email"] == account["email"]
    assert body["workspace_id"] == str(account["workspace_id"])
    assert body["role"] == "member"


def test_me_ignores_a_workspace_the_user_does_not_belong_to(account, db_session):
    """A client-supplied workspace id grants nothing on its own."""
    other_workspace = Workspace(name="other-%s" % uuid.uuid4().hex)
    db_session.add(other_workspace)
    db_session.flush()

    r = client.get(
        "/api/v1/auth/me",
        headers={
            **_auth_header(account["user_id"]),
            WORKSPACE_HEADER: str(other_workspace.id),
        },
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


def test_me_accepts_a_workspace_the_user_does_belong_to(account, db_session):
    """A user may belong to several workspaces and select one."""
    second = Workspace(name="second-%s" % uuid.uuid4().hex)
    db_session.add(second)
    db_session.flush()
    db_session.add(
        WorkspaceMembership(
            user_id=account["user_id"],
            workspace_id=second.id,
            role="admin",
        )
    )
    db_session.flush()

    r = client.get(
        "/api/v1/auth/me",
        headers={
            **_auth_header(account["user_id"]),
            WORKSPACE_HEADER: str(second.id),
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["workspace_id"] == str(second.id)
    assert body["role"] == "admin"


def test_me_requires_a_workspace_choice_when_the_user_has_several(account, db_session):
    second = Workspace(name="second-%s" % uuid.uuid4().hex)
    db_session.add(second)
    db_session.flush()
    db_session.add(
        WorkspaceMembership(
            user_id=account["user_id"],
            workspace_id=second.id,
            role="member",
        )
    )
    db_session.flush()

    r = client.get("/api/v1/auth/me", headers=_auth_header(account["user_id"]))
    assert r.status_code == 403
    assert WORKSPACE_HEADER in r.json()["error"]["message"]


def test_me_rejects_a_malformed_workspace_header(account):
    r = client.get(
        "/api/v1/auth/me",
        headers={**_auth_header(account["user_id"]), WORKSPACE_HEADER: "not-a-uuid"},
    )
    assert r.status_code == 403


def test_me_rejects_a_user_without_any_workspace(db_session):
    orphan = User(
        email="orphan-%s@example.com" % uuid.uuid4().hex,
        password_hash="$2b$12$APB7JBc6bwnP9us0otNUAe4MlRYIcY0azrFhCdpCYuxWm.CxSjaI.",
    )
    db_session.add(orphan)
    db_session.flush()

    r = client.get("/api/v1/auth/me", headers=_auth_header(orphan.id))
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# require_role
# ---------------------------------------------------------------------------

def _protected_app(current_user_dependency):
    """Build a tiny app exposing a role-gated route."""
    router = APIRouter()

    @router.get("/_test/admin-only")
    def admin_only(user=Depends(current_user_dependency)):
        return {"ok": True, "role": user.role}

    probe = FastAPI()
    # The probe needs the same structured error handlers as the real app,
    # otherwise a ForbiddenError would surface as a 500 instead of a 403.
    register_error_handlers(probe)
    probe.include_router(router)
    # The probe app must use the test's database session too, otherwise it
    # would open its own connection and not see the test's rows.
    probe.dependency_overrides.update(app.dependency_overrides)
    return TestClient(probe, raise_server_exceptions=False)


def test_require_role_allows_a_sufficient_role(account, db_session):
    from app.auth.dependencies import require_role

    db_session.execute(
        select(WorkspaceMembership).where(
            WorkspaceMembership.user_id == account["user_id"]
        )
    )
    membership = db_session.execute(
        select(WorkspaceMembership).where(
            WorkspaceMembership.user_id == account["user_id"]
        )
    ).scalar_one()
    membership.role = "admin"
    db_session.flush()

    probe = _protected_app(require_role("admin"))
    r = probe.get("/_test/admin-only", headers=_auth_header(account["user_id"]))
    assert r.status_code == 200
    assert r.json()["role"] == "admin"


def test_require_role_blocks_an_insufficient_role(account):
    from app.auth.dependencies import require_role

    probe = _protected_app(require_role("admin"))
    r = probe.get("/_test/admin-only", headers=_auth_header(account["user_id"]))
    assert r.status_code == 403


def test_require_role_member_allows_admin(account, db_session):
    """A member-level gate must not lock out admins."""
    from app.auth.dependencies import require_role

    membership = db_session.execute(
        select(WorkspaceMembership).where(
            WorkspaceMembership.user_id == account["user_id"]
        )
    ).scalar_one()
    membership.role = "owner"
    db_session.flush()

    probe = _protected_app(require_role("member"))
    r = probe.get("/_test/admin-only", headers=_auth_header(account["user_id"]))
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------

def test_login_rejects_a_bad_body():
    r = client.post("/api/v1/auth/login", json={"email": "nope", "password": "x"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_login_with_a_wrong_password_returns_a_generic_401(account):
    r = client.post(
        "/api/v1/auth/login",
        json={"email": account["email"], "password": "irrelevant"},
    )
    # The seeded account has a dummy password hash, so login must fail -
    # but the error must be the generic one.
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"
    assert r.json()["error"]["message"] == "Invalid email or password."


def test_login_with_an_unknown_email_returns_the_same_generic_401(account):
    """An unknown email and a wrong password must be indistinguishable."""
    r = client.post(
        "/api/v1/auth/login",
        json={"email": "nobody-%s@example.com" % uuid.uuid4().hex, "password": "x" * 12},
    )
    assert r.status_code == 401
    assert r.json()["error"]["message"] == "Invalid email or password."


def test_login_returns_a_token_and_the_users_workspaces(db_session):
    from app.auth.service import create_user

    email = "login-%s@example.com" % uuid.uuid4().hex
    user = create_user(db_session, email, "my-password-123")
    workspace = Workspace(name="login-ws-%s" % uuid.uuid4().hex)
    db_session.add(workspace)
    db_session.flush()
    db_session.add(
        WorkspaceMembership(
            user_id=user.id, workspace_id=workspace.id, role="admin"
        )
    )
    db_session.flush()

    r = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "my-password-123"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["workspaces"] == [
        {"workspace_id": str(workspace.id), "role": "admin"}
    ]

    # The token from the login response must work on a protected route.
    r2 = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer %s" % body["access_token"]},
    )
    assert r2.status_code == 200
    assert r2.json()["workspace_id"] == str(workspace.id)


def test_logout_is_a_no_op_for_stateless_tokens():
    r = client.post("/api/v1/auth/logout")
    assert r.status_code == 204
