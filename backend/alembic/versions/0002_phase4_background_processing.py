"""Add retryable document state and the sweeper index.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-25 00:00:00.000000

Phase 4 needs to know whether a failed document can be retried without
weakening the attempt cap. ``retryable`` records that decision on the row
so the retry endpoint can enforce it atomically.

The sweeper scans documents globally by state and age, which the existing
workspace-scoped index cannot serve. ``(status, updated_at)`` supports
that scan; the existing ``(workspace_id, status, created_at)`` index still
serves the per-workspace document list.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "retryable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "ix_documents_status_updated_at",
        "documents",
        ["status", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_documents_status_updated_at",
        table_name="documents",
    )
    op.drop_column("documents", "retryable")
