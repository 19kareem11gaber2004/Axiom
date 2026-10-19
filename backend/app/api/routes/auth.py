"""Authentication routes: login, current principal, logout."""

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.api.errors import AuthenticationError
from app.api.schemas import (
    CurrentUserRead,
    MembershipRead,
    TokenRequest,
    TokenResponse,
)
from app.auth.dependencies import (
    CurrentMembership,
    get_current_membership,
    list_memberships,
)
from app.auth.service import authenticate_user, create_access_token
from app.db.session import get_db

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
def login(payload: TokenRequest, db: Session = Depends(get_db)):
    """Exchange credentials for a JWT access token.

    The failure message is deliberately identical for an unknown email and
    for a wrong password, so the endpoint cannot be used to discover which
    email addresses are registered.
    """
    user = authenticate_user(db, payload.email, payload.password)
    if user is None:
        raise AuthenticationError("Invalid email or password.")

    memberships = list_memberships(db, user.id)
    return TokenResponse(
        access_token=create_access_token(user.id),
        token_type="bearer",
        workspaces=[
            MembershipRead(workspace_id=m.workspace_id, role=m.role)
            for m in memberships
        ],
    )


@router.get("/me", response_model=CurrentUserRead)
def me(membership: CurrentMembership = Depends(get_current_membership)):
    """Return the caller as resolved on the server.

    The workspace and role here come from the database, not from the
    token and not from the request.
    """
    return CurrentUserRead(
        id=membership.user.id,
        email=membership.user.email,
        workspace_id=membership.workspace_id,
        role=membership.role,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout():
    """Sign out.

    Axiom issues stateless access tokens: the server keeps no session
    list, so it cannot revoke an individual token. Logging out means the
    client discards the token. The token also stops being accepted once
    it expires, and any change to the account (membership removed, role
    changed, account deleted) takes effect on the next request because
    the workspace and role are always re-read from the database.

    A server-side token blocklist is deferred to a later phase.
    """
    return Response(status_code=status.HTTP_204_NO_CONTENT)
