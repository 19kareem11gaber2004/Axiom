"""Tests for workspace creation, listing and member management (Step 2.4).

Covers the provisioning flow: creating a workspace makes the caller its
owner, and members are added by an admin or owner.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.service import create_access_token
from app.db.models import User, Workspace, WorkspaceMembership
from app.main import app

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def user_with_workspace(db_session):
    """A user with one workspace, as the owner."""
    user = User(
        email="ws-%s@example.com" % uuid.uuid4().hex,
        password_hash="$2b$12$APB7JBc6bwnP9us0otNUAe4MlRYIcY0azrFhCdpCYuxWm.CxSjaI.",
    )
    workspace = Workspace(name="ws-%s" % uuid.uuid4().hex)
    db_session.add_all([user, workspace])
    db_session.flush()
    db_session.add(
        WorkspaceMembership(
            user_id=user.id, workspace_id=workspace.id, role="owner"
        )
    )
    db_session.flush()
    return {
        "user_id": user.id,
        "email": user.email,
        "workspace_id": workspace.id,
    }


def _auth(user_id, workspace_id=None):
    headers = {"Authorization": "Bearer %s" % create_access_token(user_id)}
    if workspace_id is not None:
        headers["X-Workspace-ID"] = str(workspace_id)
    return headers


def _set_role(db_session, user_id, role):
    """Change the caller's role in their workspace."""
    membership = db_session.execute(
        select(WorkspaceMembership).where(
            WorkspaceMembership.user_id == user_id
        )
    ).scalar_one()
    membership.role = role
    db_session.flush()
    return membership


# ---------------------------------------------------------------------------
# Creating a workspace
# ---------------------------------------------------------------------------

def test_create_workspace_requires_authentication(db_session):
    r = client.post("/api/v1/workspaces", json={"name": "New"})
    assert r.status_code == 401


def test_create_workspace_rejects_a_blank_name(user_with_workspace):
    r = client.post(
        "/api/v1/workspaces",
        json={"name": ""},
        headers=_auth(user_with_workspace["user_id"]),
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_create_workspace_makes_the_caller_the_owner(db_session):
    """A user with no workspace at all must be able to create one."""
    user = User(
        email="lonely-%s@example.com" % uuid.uuid4().hex,
        password_hash="$2b$12$APB7JBc6bwnP9us0otNUAe4MlRYIcY0azrFhCdpCYuxWm.CxSjaI.",
    )
    db_session.add(user)
    db_session.flush()

    r = client.post(
        "/api/v1/workspaces",
        json={"name": "My New Workspace"},
        headers=_auth(user.id),
    )
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "My New Workspace"

    # The creator holds the owner role in the new workspace.
    r2 = client.get(
        "/api/v1/workspaces/%s" % body["id"],
        headers=_auth(user.id, body["id"]),
    )
    assert r2.status_code == 200
    assert [m["role"] for m in r2.json()["members"]] == ["owner"]


# ---------------------------------------------------------------------------
# Listing workspaces
# ---------------------------------------------------------------------------

def test_list_workspaces_returns_only_the_callers_workspaces(
    user_with_workspace, db_session
):
    other = Workspace(name="other-%s" % uuid.uuid4().hex)
    db_session.add(other)
    db_session.flush()

    r = client.get(
        "/api/v1/workspaces", headers=_auth(user_with_workspace["user_id"])
    )
    assert r.status_code == 200
    names = [w["name"] for w in r.json()]
    assert other.name not in names
    assert len(names) == 1


def test_list_workspaces_includes_the_workspace_the_caller_belongs_to(
    user_with_workspace,
):
    r = client.get(
        "/api/v1/workspaces", headers=_auth(user_with_workspace["user_id"])
    )
    assert r.status_code == 200
    ids = [w["id"] for w in r.json()]
    assert str(user_with_workspace["workspace_id"]) in ids


def test_list_workspaces_requires_authentication():
    r = client.get("/api/v1/workspaces")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Reading a workspace
# ---------------------------------------------------------------------------

def test_get_workspace_returns_the_members(user_with_workspace):
    r = client.get(
        "/api/v1/workspaces/%s" % user_with_workspace["workspace_id"],
        headers=_auth(user_with_workspace["user_id"]),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == str(user_with_workspace["workspace_id"])
    assert len(body["members"]) == 1
    assert body["members"][0]["email"] == user_with_workspace["email"]
    assert body["members"][0]["role"] == "owner"


def test_get_workspace_hides_a_workspace_the_caller_does_not_belong_to(
    user_with_workspace, db_session
):
    other = Workspace(name="other-%s" % uuid.uuid4().hex)
    db_session.add(other)
    db_session.flush()

    r = client.get(
        "/api/v1/workspaces/%s" % other.id,
        headers=_auth(user_with_workspace["user_id"]),
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


# ---------------------------------------------------------------------------
# Adding members
# ---------------------------------------------------------------------------

def test_owner_can_add_a_member_and_the_account_is_created(
    user_with_workspace, db_session
):
    new_email = "invited-%s@example.com" % uuid.uuid4().hex
    r = client.post(
        "/api/v1/workspaces/%s/members" % user_with_workspace["workspace_id"],
        json={"email": new_email, "role": "member", "password": "initial-pass-123"},
        headers=_auth(user_with_workspace["user_id"]),
    )
    assert r.status_code == 201
    body = r.json()
    assert body["email"] == new_email
    assert body["role"] == "member"

    from sqlalchemy import select

    user = db_session.execute(
        select(User).where(User.email == new_email)
    ).scalar_one()
    assert user.password_hash != "initial-pass-123"
    assert user.password_hash.startswith("$2b$")


def test_adding_an_existing_user_only_creates_the_membership(
    user_with_workspace, db_session
):
    existing = User(
        email="existing-%s@example.com" % uuid.uuid4().hex,
        password_hash="$2b$12$APB7JBc6bwnP9us0otNUAe4MlRYIcY0azrFhCdpCYuxWm.CxSjaI.",
    )
    db_session.add(existing)
    db_session.flush()
    original_hash = existing.password_hash

    r = client.post(
        "/api/v1/workspaces/%s/members" % user_with_workspace["workspace_id"],
        json={
            "email": existing.email,
            "role": "viewer",
            "password": "should-be-ignored-999",
        },
        headers=_auth(user_with_workspace["user_id"]),
    )
    assert r.status_code == 201

    # The existing account's password must not have been overwritten.
    db_session.expire(existing)
    assert existing.password_hash == original_hash


def test_adding_a_duplicate_member_is_a_conflict(user_with_workspace):
    payload = {
        "email": "dup-%s@example.com" % uuid.uuid4().hex,
        "role": "member",
        "password": "initial-pass-123",
    }
    url = "/api/v1/workspaces/%s/members" % user_with_workspace["workspace_id"]
    r1 = client.post(url, json=payload, headers=_auth(user_with_workspace["user_id"]))
    assert r1.status_code == 201

    r2 = client.post(url, json=payload, headers=_auth(user_with_workspace["user_id"]))
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "INVALID_STATE"


def test_a_member_cannot_add_other_members(user_with_workspace, db_session):
    """Only admin and owner may invite people."""
    _set_role(db_session, user_with_workspace["user_id"], "member")

    r = client.post(
        "/api/v1/workspaces/%s/members" % user_with_workspace["workspace_id"],
        json={
            "email": "x-%s@example.com" % uuid.uuid4().hex,
            "role": "member",
            "password": "initial-pass-123",
        },
        headers=_auth(user_with_workspace["user_id"]),
    )
    assert r.status_code == 403


def test_a_viewer_cannot_add_other_members(user_with_workspace, db_session):
    _set_role(db_session, user_with_workspace["user_id"], "viewer")

    r = client.post(
        "/api/v1/workspaces/%s/members" % user_with_workspace["workspace_id"],
        json={
            "email": "x-%s@example.com" % uuid.uuid4().hex,
            "role": "member",
            "password": "initial-pass-123",
        },
        headers=_auth(user_with_workspace["user_id"]),
    )
    assert r.status_code == 403


def test_the_owner_role_cannot_be_granted(user_with_workspace):
    """Ownership is not transferable through the members API in v0.1."""
    r = client.post(
        "/api/v1/workspaces/%s/members" % user_with_workspace["workspace_id"],
        json={
            "email": "x-%s@example.com" % uuid.uuid4().hex,
            "role": "owner",
            "password": "initial-pass-123",
        },
        headers=_auth(user_with_workspace["user_id"]),
    )
    # The schema rejects "owner" before the service is ever called.
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_adding_a_member_to_a_workspace_the_caller_does_not_belong_to(
    user_with_workspace, db_session
):
    other = Workspace(name="other-%s" % uuid.uuid4().hex)
    db_session.add(other)
    db_session.flush()

    r = client.post(
        "/api/v1/workspaces/%s/members" % other.id,
        json={
            "email": "x-%s@example.com" % uuid.uuid4().hex,
            "role": "member",
            "password": "initial-pass-123",
        },
        headers=_auth(user_with_workspace["user_id"]),
    )
    assert r.status_code == 403
