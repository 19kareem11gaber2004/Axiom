"""Workspace routes: create, list, inspect, and manage members.

Workspace creation is the provisioning entry point. Axiom has no public
registration: an administrator adds people to a workspace, and the
account is created on the spot if the email is not registered yet.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.errors import ConflictError, ValidationError
from app.api.schemas import (
    MemberCreate,
    WorkspaceCreate,
    WorkspaceDetailRead,
    WorkspaceMemberRead,
    WorkspaceRead,
)
from app.auth.dependencies import (
    CurrentMembership,
    CurrentUser,
    get_current_user,
    require_workspace_role,
)
from app.auth.service import (
    InvalidRoleError,
    MembershipAlreadyExistsError,
    add_workspace_member,
    create_workspace,
)
from app.db.models import User, Workspace, WorkspaceMembership
from app.db.session import get_db

router = APIRouter(prefix="/api/v1/workspaces", tags=["workspaces"])


@router.post("", response_model=WorkspaceRead, status_code=status.HTTP_201_CREATED)
def create_workspace_endpoint(
    payload: WorkspaceCreate,
    current_user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a workspace; the caller becomes its owner.

    This only needs an authenticated identity, not an existing
    membership - it is the endpoint a brand-new user calls first.
    """
    workspace = create_workspace(db, payload.name, current_user.id)
    return WorkspaceRead(
        id=workspace.id,
        name=workspace.name,
        created_at=workspace.created_at,
    )


@router.get("", response_model=list[WorkspaceRead])
def list_workspaces(
    current_user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List the workspaces the caller belongs to."""
    workspaces = db.execute(
        select(Workspace)
        .join(
            WorkspaceMembership,
            WorkspaceMembership.workspace_id == Workspace.id,
        )
        .where(WorkspaceMembership.user_id == current_user.id)
        .order_by(Workspace.created_at)
    ).scalars().all()

    return [
        WorkspaceRead(id=w.id, name=w.name, created_at=w.created_at)
        for w in workspaces
    ]


@router.get("/{workspace_id}", response_model=WorkspaceDetailRead)
def get_workspace(
    workspace_id: UUID,
    membership: CurrentMembership = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    """Return a workspace and its members.

    Membership is checked against the workspace named in the path, so a
    caller cannot read a workspace they do not belong to.
    """
    rows = db.execute(
        select(User, WorkspaceMembership)
        .join(
            WorkspaceMembership,
            WorkspaceMembership.user_id == User.id,
        )
        .where(WorkspaceMembership.workspace_id == workspace_id)
        .order_by(WorkspaceMembership.created_at)
    ).all()

    workspace = db.execute(
        select(Workspace).where(Workspace.id == workspace_id)
    ).scalar_one()

    return WorkspaceDetailRead(
        id=workspace.id,
        name=workspace.name,
        created_at=workspace.created_at,
        members=[
            WorkspaceMemberRead(
                id=user.id,
                email=user.email,
                role=wm.role,
                created_at=wm.created_at,
            )
            for user, wm in rows
        ],
    )


@router.post(
    "/{workspace_id}/members",
    response_model=WorkspaceMemberRead,
    status_code=status.HTTP_201_CREATED,
)
def add_member(
    workspace_id: UUID,
    payload: MemberCreate,
    membership: CurrentMembership = Depends(require_workspace_role("admin")),
    db: Session = Depends(get_db),
):
    """Add a member to a workspace, creating the account if needed.

    Requires admin or owner in this workspace. A viewer or member cannot
    invite people.
    """
    try:
        user = add_workspace_member(
            db,
            workspace_id=workspace_id,
            email=payload.email,
            role=payload.role,
            password=payload.password,
        )
    except MembershipAlreadyExistsError as exc:
        raise ConflictError(str(exc)) from exc
    except InvalidRoleError as exc:
        raise ValidationError(str(exc)) from exc

    wm = db.execute(
        select(WorkspaceMembership).where(
            WorkspaceMembership.user_id == user.id,
            WorkspaceMembership.workspace_id == workspace_id,
        )
    ).scalar_one()

    return WorkspaceMemberRead(
        id=user.id,
        email=user.email,
        role=wm.role,
        created_at=wm.created_at,
    )
