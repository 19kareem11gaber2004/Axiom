"""FastAPI dependencies for authentication and server-side authorization.

The governing rule of this module: **the client is never trusted.**

An access token proves *who* the caller is. The database decides *what*
they may do. The token deliberately contains no workspace and no role
(see app/auth/service.py), so:

- a user who is removed from a workspace loses access on their very next
  request, not when their token happens to expire;
- a user who is demoted from admin to member loses admin rights
  immediately;
- a caller cannot widen their own scope by putting a workspace id in a
  request body, a query string, or even a header. The header value is
  only ever used to *select* among workspaces the user really belongs to;
  it is checked against workspace_memberships before it is believed.
"""

import uuid
from dataclasses import dataclass
from typing import Optional, Tuple

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.errors import AuthenticationError, ForbiddenError
from app.auth.service import TokenError, decode_access_token
from app.db.models import User, WorkspaceMembership
from app.db.session import get_db

# auto_error=False so that a missing header raises our own AxiomError
# (consistent error envelope) instead of FastAPI's default {"detail": ...}.
bearer_scheme = HTTPBearer(auto_error=False)

# Optional header a client may send to select one of several workspaces
# it belongs to. The value is verified against the database; it never
# grants access on its own.
WORKSPACE_HEADER = "X-Workspace-ID"

# Relative authority of each workspace role.
#
# In v0.1 owner and admin are equivalent (see docs/phase-1/03-erd-api.md
# section 5); the only difference is workspace management, which is a
# later-phase feature.
ROLE_RANK = {
    "viewer": 0,
    "member": 1,
    "admin": 2,
    "owner": 2,
}


@dataclass(frozen=True)
class CurrentUser:
    """The authenticated identity for one request.

    Identity only: who the caller is. It deliberately carries no
    workspace and no role, because a signed-in user with no workspace
    membership is still a legitimate user - they just may not have
    created one yet.
    """

    id: uuid.UUID
    email: str


@dataclass(frozen=True)
class CurrentMembership:
    """The caller's membership in one workspace."""

    user: CurrentUser
    workspace_id: uuid.UUID
    role: str


def list_memberships(db: Session, user_id: uuid.UUID) -> list[WorkspaceMembership]:
    """Return every workspace membership the user holds."""
    return list(
        db.execute(
            select(WorkspaceMembership).where(
                WorkspaceMembership.user_id == user_id
            )
        ).scalars()
    )


def _load_user(db: Session, user_id: uuid.UUID) -> Optional[User]:
    return db.execute(
        select(User).where(User.id == user_id)
    ).scalar_one_or_none()


def _resolve_workspace(
    db: Session,
    user_id: uuid.UUID,
    requested_workspace: Optional[str],
) -> Tuple[uuid.UUID, str]:
    """Decide which workspace the request operates in.

    Returns the (workspace_id, role) pair the caller is allowed to use.
    """
    if requested_workspace:
        try:
            workspace_id = uuid.UUID(requested_workspace)
        except (ValueError, AttributeError) as exc:
            raise ForbiddenError("Invalid workspace identifier.") from exc

        membership = db.execute(
            select(WorkspaceMembership).where(
                WorkspaceMembership.user_id == user_id,
                WorkspaceMembership.workspace_id == workspace_id,
            )
        ).scalar_one_or_none()

        if membership is None:
            # The workspace id may be real but simply not ours. Say
            # "not a member" without confirming that it exists.
            raise ForbiddenError("You are not a member of this workspace.")
        return membership.workspace_id, membership.role

    memberships = list_memberships(db, user_id)

    if not memberships:
        raise ForbiddenError("Your account is not a member of any workspace.")
    if len(memberships) > 1:
        raise ForbiddenError(
            "Your account belongs to several workspaces. Send the %s header "
            "to choose one." % WORKSPACE_HEADER
        )

    membership = memberships[0]
    return membership.workspace_id, membership.role


def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> CurrentUser:
    """Resolve the authenticated caller, or raise 401.

    Run this for every protected route. It verifies the access token and
    confirms the account still exists. It does not require a workspace:
    listing and creating workspaces must work for a user who has none yet.
    """
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Authentication credentials were not provided.")
    if (credentials.scheme or "").lower() != "bearer":
        raise AuthenticationError("Unsupported authentication scheme.")

    try:
        payload = decode_access_token(credentials.credentials)
    except TokenError as exc:
        raise AuthenticationError(str(exc)) from exc

    # A valid signature only proves the token was issued by us. The
    # account may since have been deleted, so the user is looked up.
    user = _load_user(db, payload.sub)
    if user is None:
        raise AuthenticationError("Authentication credentials are invalid.")

    return CurrentUser(id=user.id, email=user.email)


def get_current_membership(
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CurrentMembership:
    """Resolve the caller's membership in the workspace they are acting in.

    The workspace comes from the X-Workspace-ID header, or from the only
    membership the caller has. The header is verified against the
    database; it never grants access on its own.
    """
    workspace_id, role = _resolve_workspace(
        db, current_user.id, request.headers.get(WORKSPACE_HEADER)
    )
    return CurrentMembership(
        user=current_user, workspace_id=workspace_id, role=role
    )


def get_workspace_membership(
    workspace_id: uuid.UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CurrentMembership:
    """Resolve the caller's membership in a specific workspace.

    The workspace id is taken from the route's path parameter, so it is
    the workspace the resource actually lives in - not one the caller
    chose in a header. Used for routes such as
    /workspaces/{workspace_id}/members.
    """
    membership = db.execute(
        select(WorkspaceMembership).where(
            WorkspaceMembership.user_id == current_user.id,
            WorkspaceMembership.workspace_id == workspace_id,
        )
    ).scalar_one_or_none()

    if membership is None:
        raise ForbiddenError("You are not a member of this workspace.")

    return CurrentMembership(
        user=current_user,
        workspace_id=membership.workspace_id,
        role=membership.role,
    )


def _rank_of(roles) -> int:
    return min(ROLE_RANK[role] for role in roles)


def ensure_role(membership: CurrentMembership, *allowed_roles: str) -> None:
    """Raise 403 unless the membership holds a sufficient role.

    Used by routes that must check the resource *before* the role, so
    that a document in another workspace always answers 404 regardless
    of the caller's role. Otherwise the response would depend on the
    caller rather than on the resource.
    """
    if ROLE_RANK.get(membership.role, -1) < _rank_of(allowed_roles):
        raise ForbiddenError("Your workspace role does not permit this action.")


def require_role(*allowed_roles: str):
    """Dependency factory that enforces a minimum role in the current workspace.

    Usage:
        @router.delete("/documents/{id}")
        def delete_document(id: UUID, user: CurrentMembership = Depends(require_role("admin"))):
            ...

    Because owner and admin are equivalent in v0.1, require_role("admin")
    accepts both. A caller with a lower rank is rejected with 403.

    Note: this checks the role *before* the route body runs. For routes
    that act on one specific resource, prefer get_current_membership plus
    ensure_role() so that a resource in another workspace always returns
    404 no matter which role the caller holds.
    """
    if not allowed_roles:
        raise ValueError("require_role() needs at least one role.")
    unknown = [role for role in allowed_roles if role not in ROLE_RANK]
    if unknown:
        raise ValueError("Unknown role(s): %s" % ", ".join(sorted(unknown)))

    required_rank = _rank_of(allowed_roles)

    def dependency(
        membership: CurrentMembership = Depends(get_current_membership),
    ) -> CurrentMembership:
        if ROLE_RANK.get(membership.role, -1) < required_rank:
            raise ForbiddenError(
                "Your workspace role does not permit this action."
            )
        return membership

    return dependency


def require_workspace_role(*allowed_roles: str):
    """Dependency factory enforcing a minimum role in the path's workspace.

    The workspace comes from the route path, so a caller cannot point a
    path-scoped route at a different workspace through a header.
    """
    if not allowed_roles:
        raise ValueError("require_workspace_role() needs at least one role.")
    unknown = [role for role in allowed_roles if role not in ROLE_RANK]
    if unknown:
        raise ValueError("Unknown role(s): %s" % ", ".join(sorted(unknown)))

    required_rank = _rank_of(allowed_roles)

    def dependency(
        membership: CurrentMembership = Depends(get_workspace_membership),
    ) -> CurrentMembership:
        if ROLE_RANK.get(membership.role, -1) < required_rank:
            raise ForbiddenError(
                "Your workspace role does not permit this action."
            )
        return membership

    return dependency
