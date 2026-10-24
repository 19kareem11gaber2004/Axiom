"""Pydantic schemas for Axiom API request/response validation.

Every API input and output is validated against these schemas before it
reaches business logic. This prevents malformed requests from corrupting
database state and guarantees clients receive predictable response shapes.

All ORM-compatible models use model_config = ConfigDict(from_attributes=True)
so they can be instantiated directly from SQLAlchemy instances.
"""

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ErrorDetail(BaseModel):
    """A single structured error detail.

    Returned to clients inside the top-level "error" envelope so frontend
    code can handle failures programmatically instead of parsing strings.
    """

    code: str
    message: str
    retryable: bool = False
    request_id: Optional[str] = None


class ErrorResponse(BaseModel):
    """Top-level error envelope used by all Axiom API error responses."""

    error: ErrorDetail


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

class TokenRequest(BaseModel):
    """Credentials sent by the client to obtain a JWT access token."""

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class TokenResponse(BaseModel):
    """Token issued after successful authentication."""

    access_token: str
    token_type: str = "bearer"
    # The workspaces this account can act in, so the client knows whether
    # it must send the X-Workspace-ID header on later requests.
    workspaces: list["MembershipRead"] = []


class MembershipRead(BaseModel):
    """A workspace the caller belongs to, and the role held there."""

    workspace_id: UUID
    role: str


class CurrentUserRead(BaseModel):
    """The resolved principal for the current request."""

    id: UUID
    email: EmailStr
    workspace_id: UUID
    role: str


TokenResponse.model_rebuild()


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

class UserRead(BaseModel):
    """Public user representation returned to authenticated clients."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    created_at: datetime


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------

class WorkspaceCreate(BaseModel):
    """Payload for creating a new workspace."""

    name: str = Field(min_length=1, max_length=200)


class WorkspaceRead(BaseModel):
    """Public workspace representation."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    created_at: datetime


class WorkspaceMemberRead(BaseModel):
    """One member of a workspace."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    role: str
    created_at: datetime


class WorkspaceDetailRead(BaseModel):
    """A workspace together with its membership list."""

    id: UUID
    name: str
    created_at: datetime
    members: list[WorkspaceMemberRead] = []


class MemberCreate(BaseModel):
    """Add a member to a workspace.

    If the email is not registered yet, the account is created with
    `password`. If the account already exists, `password` is ignored and
    only the membership is added.
    """

    email: EmailStr
    role: Literal["viewer", "member", "admin"] = "member"
    password: Optional[str] = Field(default=None, min_length=8, max_length=72)


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

class DocumentRead(BaseModel):
    """Full document metadata returned by detail and list endpoints."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    uploaded_by: UUID
    original_filename: str
    storage_key: str
    mime_type: str
    size_bytes: int
    checksum: str
    status: str
    processing_attempts: int
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    indexed_at: Optional[datetime] = None


class DocumentListItem(BaseModel):
    """Lightweight document summary for list views."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    original_filename: str
    status: str
    processing_attempts: int
    error_code: Optional[str] = None
    created_at: datetime


class DocumentListResponse(BaseModel):
    """A page of documents plus the total number matching the filter."""

    items: list[DocumentListItem] = []
    total: int


class DocumentUploadResponse(BaseModel):
    """Response returned immediately after a document is queued for processing.

    ``duplicate`` is true only when an existing document with the same
    checksum was returned instead of creating a new one. It defaults to
    false so the retry endpoint keeps its existing response shape.
    """

    document_id: UUID
    status: str
    duplicate: bool = False


# ---------------------------------------------------------------------------
# Conversations (skeleton for Phase 2 chat routes)
# ---------------------------------------------------------------------------

class ConversationRead(BaseModel):
    """Conversation metadata."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    title: Optional[str] = None
    created_at: datetime
