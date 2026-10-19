"""Authentication routes: login, current principal, logout."""

from typing import Optional

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.orm import Session

from app.api.errors import (
    AuthenticationError,
    DependencyUnavailableError,
    RateLimitError,
)
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
from app.auth.rate_limit import (
    check_login_allowed,
    record_login_failure,
    reset_login_failures,
)
from app.auth.service import authenticate_user, create_access_token
from app.db.session import get_db

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _client_ip(request: Request) -> Optional[str]:
    """The caller's source address.

    Behind a reverse proxy this is the proxy's address; honouring
    X-Forwarded-For is a deployment concern and is deliberately not
    trusted here, because a client could otherwise spoof the header to
    escape the per-IP limit.
    """
    client = request.client
    return client.host if client else None


@router.post("/login", response_model=TokenResponse)
def login(payload: TokenRequest, request: Request, db: Session = Depends(get_db)):
    """Exchange credentials for a JWT access token.

    The failure message is deliberately identical for an unknown email and
    for a wrong password, so the endpoint cannot be used to discover which
    email addresses are registered.

    Repeated failures are rate limited per account and per source IP; the
    limit is cleared by a successful login.

    When the limiter cannot reach Redis the configured failure mode
    decides what happens (see app/auth/rate_limit.py): the default
    "open" mode lets the attempt through, because the credential check
    below still applies; the "closed" mode answers 503 and performs no
    login at all.
    """
    ip_address = _client_ip(request)

    decision = check_login_allowed(payload.email, ip_address)
    if not decision.allowed:
        if not decision.limiter_available:
            # The limiter is down and the deployment fails closed. This
            # is a server-side problem, not a caller problem, so it is
            # reported as 503 and not as 429.
            raise DependencyUnavailableError(
                "Login is temporarily unavailable because the rate limiter "
                "cannot be reached."
            )
        raise RateLimitError(
            "Too many failed login attempts. Try again in %d seconds."
            % decision.retry_after
        )

    user = authenticate_user(db, payload.email, payload.password)
    if user is None:
        record_login_failure(payload.email, ip_address)
        raise AuthenticationError("Invalid email or password.")

    reset_login_failures(payload.email, ip_address)

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
