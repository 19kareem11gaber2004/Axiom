"""Create initial v0.1 schema.

Revision ID: 0001
Revises:
Create Date: 2026-10-18 00:00:00.000000

Aligned to docs/phase-1/03-erd-api.md section 2 and backend/app/db/models.py.

Tables:
- users, workspaces, workspace_memberships
- documents, processing_runs, chunks
- conversations, messages, citations

All user-data tables carry workspace_id. Cross-workspace references are
prevented at the database level via composite foreign keys.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Enable pgvector extension.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Enum types.
    #
    # create_type=False disables SQLAlchemy's automatic "CREATE TYPE" emission
    # when a column using the type is created. Without it, each op.create_table()
    # that references an enum column emits a second CREATE TYPE for the same
    # name, and PostgreSQL rejects the migration with
    # 'ERROR: type "document_status" already exists'.
    #
    # The types are created exactly once, explicitly, below - which keeps the
    # creation point deterministic and guarantees every type exists before the
    # tables that depend on it. This is the pattern recommended by SQLAlchemy
    # for migrations rendered to SQL without database access.
    document_status = sa.Enum(
        "uploaded",
        "queued",
        "processing",
        "retrying",
        "indexed",
        "failed",
        name="document_status",
        create_type=False,
    )
    membership_role = sa.Enum(
        "owner", "admin", "member", "viewer", name="membership_role", create_type=False
    )
    message_role = sa.Enum(
        "user", "assistant", name="message_role", create_type=False
    )

    document_status.create(op.get_bind(), checkfirst=False)
    membership_role.create(op.get_bind(), checkfirst=False)
    message_role.create(op.get_bind(), checkfirst=False)

    # users
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    # workspaces
    op.create_table(
        "workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workspaces"),
    )

    # workspace_memberships
    op.create_table(
        "workspace_memberships",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "role",
            membership_role,
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_memberships_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_memberships_workspaces",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "user_id", "workspace_id", name="pk_workspace_memberships"
        ),
    )

    # documents
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "workspace_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "uploaded_by", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "original_filename", sa.String(length=255), nullable=False
        ),
        sa.Column(
            "storage_key", sa.String(length=512), nullable=False
        ),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            document_status,
            nullable=False,
            server_default="uploaded",
        ),
        sa.Column(
            "processing_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_documents_workspaces",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_documents"),
        sa.UniqueConstraint(
            "storage_key", name="uq_documents_storage_key"
        ),
        sa.UniqueConstraint(
            "id", "workspace_id", name="uq_documents_id_workspace"
        ),
    )
    op.create_index(
        "ix_documents_ws_status_created",
        "documents",
        ["workspace_id", "status", "created_at"],
    )
    op.create_index(
        "ix_documents_ws_checksum", "documents", ["workspace_id", "checksum"]
    )

    # processing_runs
    op.create_table(
        "processing_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "document_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "workspace_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["documents.id", "documents.workspace_id"],
            name="fk_runs_documents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_processing_runs"),
        sa.UniqueConstraint(
            "document_id", "attempt_no", name="uq_runs_doc_attempt"
        ),
    )

    # chunks
    op.create_table(
        "chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "document_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "workspace_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column(
            "source_type", sa.String(length=32), nullable=False
        ),
        sa.Column(
            "chunk_version", sa.String(length=32), nullable=False
        ),
        sa.Column(
            "embedding_model", sa.String(length=128), nullable=False
        ),
        sa.Column(
            "embedding_dim", sa.Integer(), nullable=False
        ),
        sa.Column(
            "embedding", Vector(768), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["documents.id", "documents.workspace_id"],
            name="fk_chunks_documents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chunks"),
        sa.UniqueConstraint(
            "document_id", "chunk_index", name="uq_chunks_doc_index"
        ),
        sa.CheckConstraint(
            "chunk_index >= 0", name="ck_chunks_chunk_index_nonneg"
        ),
        sa.CheckConstraint(
            "embedding_dim > 0", name="ck_chunks_embedding_dim_positive"
        ),
    )
    op.create_index("ix_chunks_workspace", "chunks", ["workspace_id"])

    # conversations
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "workspace_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("title", sa.String(length=300), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_conversations_workspaces",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_conversations"),
        sa.UniqueConstraint(
            "id", "workspace_id", name="uq_conversations_id_workspace"
        ),
    )
    op.create_index(
        "ix_conversations_ws_created",
        "conversations",
        ["workspace_id", "created_at"],
    )

    # messages
    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "conversation_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "workspace_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "role",
            message_role,
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("tokens", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "workspace_id"],
            ["conversations.id", "conversations.workspace_id"],
            name="fk_messages_conversations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_messages"),
        sa.UniqueConstraint(
            "id", "workspace_id", name="uq_messages_id_workspace"
        ),
    )
    op.create_index(
        "ix_messages_conv_created",
        "messages",
        ["conversation_id", "created_at"],
    )

    # citations
    op.create_table(
        "citations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "message_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "workspace_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "document_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "original_filename", sa.String(length=255), nullable=False
        ),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("passage_text", sa.Text(), nullable=False),
        sa.Column("score", sa.Numeric(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["message_id", "workspace_id"],
            ["messages.id", "messages.workspace_id"],
            name="fk_citations_messages",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["chunks.id"],
            name="fk_citations_chunk",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_citations"),
    )
    op.create_index("ix_citations_message", "citations", ["message_id"])


def downgrade() -> None:
    op.drop_index("ix_citations_message", table_name="citations")
    op.drop_table("citations")

    op.drop_index("ix_messages_conv_created", table_name="messages")
    op.drop_table("messages")

    op.drop_index("ix_conversations_ws_created", table_name="conversations")
    op.drop_table("conversations")

    op.drop_index("ix_chunks_workspace", table_name="chunks")
    op.drop_table("chunks")

    op.drop_table("processing_runs")

    op.drop_index("ix_documents_ws_checksum", table_name="documents")
    op.drop_index("ix_documents_ws_status_created", table_name="documents")
    op.drop_table("documents")

    op.drop_table("workspace_memberships")
    op.drop_table("workspaces")
    op.drop_table("users")

    # Drop enum types.
    op.execute("DROP TYPE IF EXISTS message_role")
    op.execute("DROP TYPE IF EXISTS membership_role")
    op.execute("DROP TYPE IF EXISTS document_status")

    op.execute("DROP EXTENSION IF EXISTS vector")
