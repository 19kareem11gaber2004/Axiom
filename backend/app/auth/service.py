"""Authentication service: password hashing, JWT access tokens, credential checks.

This module is deliberately HTTP-agnostic. It raises plain domain
exceptions (InvalidCredentialsError, TokenError) and never touches
FastAPI or HTTP status codes; the API layer maps them onto the
structured error envelope defined in app/api/errors.py.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import User, Workspace, WorkspaceMembership

# Roles that may be granted to a member. "owner" is not in this list: the
# creator of a workspace is its owner, and ownership is not transferable
# through the member-management API in v0.1.
GRANTABLE_ROLES = ("viewer", "member", "admin")

# bcrypt only ever considers the first 72 bytes of a password, and the
# bcrypt library refuses to hash anything longer rather than silently
# truncating it. We check the byte length explicitly so the failure is
# reported as a clear validation error instead of a library error.
MAX_PASSWORD_BYTES = 72

# A precomputed bcrypt hash of a random throwaway value. It is used to
# spend a comparable amount of CPU time when a login is attempted for an
# email that does not exist, so that response timing does not reveal
# which email addresses are registered. The plaintext behind it was
# discarded when it was generated and is not stored anywhere.
_DUMMY_HASH = "$2b$12$APB7JBc6bwnP9us0otNUAe4MlRYIcY0azrFhCdpCYuxWm.CxSjaI."


class AuthError(Exception):
    """Base class for authentication failures raised by this module."""


class InvalidCredentialsError(AuthError):
    """The supplied email/password combination is not valid."""


class UserAlreadyExistsError(AuthError):
    """A user with this email address already exists."""


class MembershipAlreadyExistsError(AuthError):
    """The user is already a member of this workspace."""


class InvalidRoleError(AuthError):
    """The requested role is not one that can be granted."""


class TokenError(AuthError):
    """The access token is missing, malformed, expired, or not ours."""


class TokenPayload(BaseModel):
    """The claims we expect to find inside an access token."""

    sub: uuid.UUID
    exp: datetime
    iat: datetime
    type: str = "access"


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt.

    Returns the hash as text (bcrypt works on bytes, but the hash is
    stored in a VARCHAR column and handed to bcrypt again on verification).

    Raises:
        ValueError: if the password exceeds bcrypt's 72-byte limit.
    """
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise ValueError(
            "Password must be at most %d bytes when UTF-8 encoded."
            % MAX_PASSWORD_BYTES
        )
    salt = bcrypt.gensalt(rounds=settings.bcrypt_rounds)
    return bcrypt.hashpw(encoded, salt).decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    """Check a plaintext password against a stored bcrypt hash.

    Returns False instead of raising for a malformed hash or an
    over-long candidate: during login, both simply mean "not a match".
    """
    encoded = plain_password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        return False
    try:
        return bcrypt.checkpw(encoded, password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def create_user(session: Session, email: str, password: str) -> User:
    """Create a user with a hashed password.

    Raises:
        UserAlreadyExistsError: if the email is already registered.
    """
    normalised = email.strip().lower()
    existing = session.execute(
        select(User).where(func.lower(User.email) == normalised)
    ).scalar_one_or_none()
    if existing is not None:
        raise UserAlreadyExistsError("A user with this email already exists.")

    user = User(email=normalised, password_hash=hash_password(password))
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def authenticate_user(session: Session, email: str, password: str) -> Optional[User]:
    """Return the user when the credentials are valid, otherwise None.

    Both "no such account" and "wrong password" return None, so the
    caller cannot use the result to discover which emails are registered.
    """
    normalised = email.strip().lower()
    user = session.execute(
        select(User).where(func.lower(User.email) == normalised)
    ).scalar_one_or_none()

    if user is None:
        # Spend comparable time on a hash check so the response time does
        # not reveal whether the account exists.
        verify_password(password, _DUMMY_HASH)
        return None

    if not verify_password(password, user.password_hash):
        return None

    return user


# ---------------------------------------------------------------------------
# Workspaces and membership
# ---------------------------------------------------------------------------

def create_workspace(
    session: Session,
    name: str,
    owner_user_id: uuid.UUID,
) -> Workspace:
    """Create a workspace and make the creator its owner.

    The workspace row and the owner's membership row are written in one
    transaction, so a failure can never leave a workspace with no owner.
    """
    workspace = Workspace(name=name.strip())
    session.add(workspace)
    session.flush()  # assigns workspace.id

    session.add(
        WorkspaceMembership(
            user_id=owner_user_id,
            workspace_id=workspace.id,
            role="owner",
        )
    )
    session.commit()
    session.refresh(workspace)
    return workspace


def add_workspace_member(
    session: Session,
    workspace_id: uuid.UUID,
    email: str,
    role: str,
    password: Optional[str] = None,
) -> User:
    """Add a member to a workspace, creating the user if necessary.

    This is Axiom's only user-provisioning path: there is no public
    registration endpoint. An administrator invites someone by email, and
    the account is created on the spot.

    If the user already exists, `password` is ignored. A password is
    never used to overwrite an existing account's credentials - that would
    be an account-takeover vector.
    """
    if role not in GRANTABLE_ROLES:
        raise InvalidRoleError(
            "Role must be one of: %s." % ", ".join(GRANTABLE_ROLES)
        )

    normalised = email.strip().lower()
    user = session.execute(
        select(User).where(func.lower(User.email) == normalised)
    ).scalar_one_or_none()

    if user is None:
        if not password:
            raise InvalidRoleError(
                "A password is required to create a new user."
            )
        user = User(email=normalised, password_hash=hash_password(password))
        session.add(user)
        session.flush()

    existing = session.execute(
        select(WorkspaceMembership).where(
            WorkspaceMembership.user_id == user.id,
            WorkspaceMembership.workspace_id == workspace_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise MembershipAlreadyExistsError(
            "That user is already a member of this workspace."
        )

    session.add(
        WorkspaceMembership(
            user_id=user.id,
            workspace_id=workspace_id,
            role=role,
        )
    )
    session.commit()
    session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Access tokens
# ---------------------------------------------------------------------------

def create_access_token(
    user_id: uuid.UUID,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """Create a signed JWT access token for a user.

    The token carries the user identity only. Workspace and role are
    deliberately NOT in the token: they are resolved from the database on
    every request, so a membership that is revoked stops working
    immediately instead of at token expiry.
    """
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + (
        expires_delta
        if expires_delta is not None
        else timedelta(minutes=settings.jwt_access_token_expire_minutes)
    )
    payload = {
        "sub": str(user_id),
        "iat": issued_at,
        "exp": expires_at,
        "type": "access",
    }
    return jwt.encode(
        payload,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


def decode_access_token(token: str) -> TokenPayload:
    """Verify a JWT access token and return its payload.

    Verifies the signature, the expiry, and that the algorithm is the one
    we expect. Pinning `algorithms` also prevents algorithm-confusion
    attacks, where a token claims a different algorithm (for example
    "none") to bypass signature checking.

    Raises:
        TokenError: if the token is invalid, expired, or malformed.
    """
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            # "require" makes the decode fail when a claim is missing,
            # so a token without an expiry can never be accepted.
            options={"require": ["exp", "sub", "iat"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Access token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("Access token is invalid.") from exc

    if claims.get("type") != "access":
        raise TokenError("Access token is invalid.")

    try:
        return TokenPayload.model_validate(claims)
    except ValidationError as exc:
        raise TokenError("Access token is invalid.") from exc
