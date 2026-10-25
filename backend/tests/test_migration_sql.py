"""Regression tests for the generated Alembic migration SQL.

These are lightweight unit tests: they render the migration to SQL offline
(``alembic upgrade head --sql``) and assert on the *semantic* content of
that SQL. No PostgreSQL instance is required.

They exist because revision 0001 originally emitted each PostgreSQL ENUM type
twice - once explicitly and once again via SQLAlchemy's automatic
``CREATE TYPE`` emission when the dependent table was created. PostgreSQL
rejects that with 'type "document_status" already exists', so the migration
failed on every real database. These tests fail if that regression returns.

What these tests prove:
  - Every shared ENUM type is created exactly once in the generated SQL.
  - The ENUM values and their order are unchanged.
  - Each ENUM type is created before the tables that depend on it.
  - The ORM models agree with the migration about type name and values.

What these tests do NOT prove:
  - That the SQL actually executes on a live PostgreSQL server.
    That requires a real database (see tests/test_smoke_integration.py).
"""

import io
import os
import re

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Boolean

from app.db.models import Document, DocumentStatus, MembershipRole, MessageRole

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CREATE_TYPE_RE = re.compile(r"CREATE TYPE (\w+) AS ENUM \(([^)]*)\)", re.IGNORECASE)


@pytest.fixture(scope="module")
def migration_sql() -> str:
    """Render the full migration chain to SQL without touching a database."""
    config = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    buffer = io.StringIO()
    config.output_buffer = buffer
    command.upgrade(config, "head", sql=True)
    return buffer.getvalue()


@pytest.fixture(scope="module")
def downgrade_sql() -> str:
    """Render the 0002 -> 0001 downgrade without touching a database."""
    config = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    buffer = io.StringIO()
    config.output_buffer = buffer
    command.downgrade(config, "head:0001", sql=True)
    return buffer.getvalue()


@pytest.fixture(scope="module")
def created_types(migration_sql: str) -> dict:
    """Map enum type name -> list of values, for every CREATE TYPE in the SQL."""
    result = {}
    for name, raw_values in CREATE_TYPE_RE.findall(migration_sql):
        values = [v.strip().strip("'") for v in raw_values.split(",")]
        result.setdefault(name, []).append(values)
    return result


def test_each_shared_enum_is_created_exactly_once(created_types: dict):
    """The core regression: no ENUM type may be created more than once.

    A duplicate CREATE TYPE makes the whole migration fail on PostgreSQL.
    """
    for name, occurrences in created_types.items():
        assert len(occurrences) == 1, (
            "PostgreSQL ENUM %r is created %d times in the generated migration; "
            "it must be created exactly once" % (name, len(occurrences))
        )


def test_all_expected_enum_types_are_created(created_types: dict):
    assert set(created_types) == {
        "document_status",
        "membership_role",
        "message_role",
    }


def test_enum_values_and_order_are_preserved(created_types: dict):
    assert created_types["document_status"][0] == [
        "uploaded",
        "queued",
        "processing",
        "retrying",
        "indexed",
        "failed",
    ]
    assert created_types["membership_role"][0] == ["owner", "admin", "member", "viewer"]
    assert created_types["message_role"][0] == ["user", "assistant"]


def test_enum_types_are_created_before_dependent_tables(migration_sql: str):
    """Every CREATE TYPE must precede the first application CREATE TABLE.

    Alembic emits its own `alembic_version` table before the migration body
    runs, so that table is excluded from this comparison.
    """
    lines = migration_sql.splitlines()
    first_create_table = next(
        (
            i
            for i, line in enumerate(lines)
            if "CREATE TABLE" in line and "alembic_version" not in line
        ),
        None,
    )
    assert first_create_table is not None, "no application CREATE TABLE found"

    for i, line in enumerate(lines):
        if "CREATE TYPE" in line and "AS ENUM" in line:
            assert i < first_create_table, (
                "ENUM type is created after the first table: %s" % line.strip()
            )


def test_enum_columns_reference_the_created_types(migration_sql: str):
    """Columns must still be typed with the PostgreSQL ENUM type names."""
    assert re.search(r"status document_status", migration_sql)
    assert re.search(r"role membership_role", migration_sql)
    assert re.search(r"role message_role", migration_sql)


def test_pgvector_setup_is_preserved(migration_sql: str):
    assert "CREATE EXTENSION IF NOT EXISTS vector" in migration_sql
    assert re.search(r"embedding VECTOR\(768\) NOT NULL", migration_sql)


def test_retryable_column_is_added_with_a_false_default(migration_sql: str):
    assert (
        "ALTER TABLE documents ADD COLUMN retryable "
        "BOOLEAN DEFAULT false NOT NULL"
    ) in migration_sql


def test_global_status_updated_at_index_is_added(migration_sql: str):
    assert (
        "CREATE INDEX ix_documents_status_updated_at "
        "ON documents (status, updated_at)"
    ) in migration_sql


def test_downgrade_removes_the_phase4_schema(downgrade_sql: str):
    assert "DROP INDEX ix_documents_status_updated_at" in downgrade_sql
    assert "ALTER TABLE documents DROP COLUMN retryable" in downgrade_sql


def test_orm_document_matches_the_phase4_migration():
    column = Document.__table__.c.retryable
    assert isinstance(column.type, Boolean)
    assert column.nullable is False
    assert column.server_default is not None
    assert str(column.server_default.arg) == "false"

    indexes = {index.name: index for index in Document.__table__.indexes}
    index = indexes["ix_documents_status_updated_at"]
    assert [indexed.name for indexed in index.columns] == [
        "status",
        "updated_at",
    ]


def test_foreign_key_delete_actions_are_preserved(migration_sql: str):
    assert "ON DELETE CASCADE" in migration_sql
    assert "ON DELETE RESTRICT" in migration_sql
    assert "ON DELETE SET NULL" in migration_sql


def test_orm_models_agree_with_migration(created_types: dict):
    """The ORM enum declarations must match what the migration creates.

    Guards against the models and the migration drifting apart (for example a
    renamed type or a reordered value list).
    """
    assert DocumentStatus.name == "document_status"
    assert list(DocumentStatus.enums) == created_types["document_status"][0]

    assert MembershipRole.name == "membership_role"
    assert list(MembershipRole.enums) == created_types["membership_role"][0]

    assert MessageRole.name == "message_role"
    assert list(MessageRole.enums) == created_types["message_role"][0]


def test_orm_models_use_native_postgres_enums():
    """The models must map to real PostgreSQL ENUM types, not VARCHAR+CHECK."""
    for enum_type in (DocumentStatus, MembershipRole, MessageRole):
        assert enum_type.native_enum is True
        assert enum_type.schema is None  # default (public) schema
