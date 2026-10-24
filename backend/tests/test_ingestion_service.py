"""Tests for the upload ingestion service (Phase 3).

The service tests use the real PostgreSQL session fixture and an
in-memory storage adapter, so no bytes are written to ``backend/storage``.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models import Document, Workspace
from app.ingestion.service import UploadResult, find_duplicate, ingest_upload
from app.ingestion.validation import InvalidUploadError
from app.storage.base import StorageUnavailableError
from app.storage.keys import storage_key_for
from tests.fakes.storage import InMemoryFileStorage

PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"


class RecordingStorage(InMemoryFileStorage):
    """In-memory storage that records saves and deletes for assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.saved: list[tuple[str, bytes]] = []
        self.deleted: list[str] = []

    def save(self, key: str, content: bytes) -> None:
        self.saved.append((key, content))
        super().save(key, content)

    def delete(self, key: str) -> bool:
        self.deleted.append(key)
        return super().delete(key)


class FailingStorage(InMemoryFileStorage):
    """Storage whose write always fails, to exercise the no-row path."""

    def __init__(self) -> None:
        super().__init__()
        self.save_calls = 0

    def save(self, key: str, content: bytes) -> None:
        self.save_calls += 1
        raise StorageUnavailableError("simulated storage outage")


class DeleteFailingStorage(RecordingStorage):
    """Storage that cannot complete the compensation delete."""

    def delete(self, key: str) -> bool:
        raise StorageUnavailableError("simulated delete outage")


class RecordingStream(io.BytesIO):
    """BytesIO that records the size passed to each read call."""

    def __init__(self, content: bytes) -> None:
        super().__init__(content)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return super().read(size)


@pytest.fixture
def workspace(db_session):
    workspace = Workspace(name="ingestion-%s" % uuid.uuid4().hex)
    db_session.add(workspace)
    db_session.flush()
    return workspace


def _document(
    workspace_id: uuid.UUID,
    checksum: str,
    status: str,
    *,
    created_at: datetime | None = None,
) -> Document:
    return Document(
        workspace_id=workspace_id,
        uploaded_by=uuid.uuid4(),
        original_filename="existing.pdf",
        storage_key=storage_key_for(uuid.uuid4()),
        mime_type="application/pdf",
        size_bytes=len(PDF_BYTES),
        checksum=checksum,
        status=status,
        created_at=created_at or datetime.now(timezone.utc),
    )


def _ingest(
    db_session,
    workspace_id: uuid.UUID,
    storage,
    *,
    content: bytes = PDF_BYTES,
    filename: str = "report.pdf",
    allow_duplicate: bool = False,
    uploaded_by: uuid.UUID | None = None,
    max_upload_bytes: int | None = None,
    stream=None,
):
    return ingest_upload(
        db_session,
        workspace_id,
        uploaded_by if uploaded_by is not None else uuid.uuid4(),
        filename,
        stream if stream is not None else io.BytesIO(content),
        allow_duplicate=allow_duplicate,
        storage=storage,
        max_upload_bytes=max_upload_bytes,
    )


def _documents_for(db_session, workspace_id: uuid.UUID) -> list[Document]:
    return db_session.execute(
        select(Document).where(Document.workspace_id == workspace_id)
    ).scalars().all()


def _snapshot(document: Document) -> dict:
    return {
        "id": document.id,
        "workspace_id": document.workspace_id,
        "uploaded_by": document.uploaded_by,
        "original_filename": document.original_filename,
        "storage_key": document.storage_key,
        "mime_type": document.mime_type,
        "size_bytes": document.size_bytes,
        "checksum": document.checksum,
        "status": document.status,
        "created_at": document.created_at,
        "updated_at": document.updated_at,
    }


# ---------------------------------------------------------------------------
# Deduplication lookup
# ---------------------------------------------------------------------------

def test_find_duplicate_is_workspace_scoped(db_session, workspace):
    other = Workspace(name="other-%s" % uuid.uuid4().hex)
    db_session.add(other)
    db_session.flush()

    checksum = "a" * 64
    existing = _document(workspace.id, checksum, "indexed")
    db_session.add(existing)
    db_session.flush()

    assert find_duplicate(db_session, workspace.id, checksum).id == existing.id
    assert find_duplicate(db_session, other.id, checksum) is None


@pytest.mark.parametrize(
    ("statuses", "expected_status"),
    [
        (("failed", "uploaded", "queued"), "queued"),
        (("failed", "uploaded"), "uploaded"),
        (("failed",), "failed"),
        (("failed", "uploaded", "processing", "indexed"), "indexed"),
    ],
)
def test_find_duplicate_prefers_healthiest_status(
    db_session, workspace, statuses, expected_status
):
    checksum = "b" * 64
    start = datetime.now(timezone.utc)

    # The healthiest row is deliberately the OLDEST row in each case, so
    # this test fails if the query orders only by created_at.
    for offset, status in enumerate(statuses):
        db_session.add(
            _document(
                workspace.id,
                checksum,
                status,
                created_at=start - timedelta(seconds=offset),
            )
        )
    db_session.flush()

    found = find_duplicate(db_session, workspace.id, checksum)

    assert found is not None
    assert found.status == expected_status


def test_duplicate_selection_prefers_indexed_over_failed(
    db_session, workspace
):
    checksum = "d" * 64
    now = datetime.now(timezone.utc)
    failed = _document(
        workspace.id,
        checksum,
        "failed",
        created_at=now + timedelta(seconds=1),
    )
    indexed = _document(
        workspace.id,
        checksum,
        "indexed",
        created_at=now,
    )
    db_session.add_all([failed, indexed])
    db_session.flush()

    found = find_duplicate(db_session, workspace.id, checksum)

    assert found is not None
    assert found.id == indexed.id


@pytest.mark.parametrize(
    ("older_status", "newer_status"),
    [
        ("queued", "processing"),
        ("processing", "retrying"),
        ("retrying", "queued"),
    ],
)
def test_find_duplicate_returns_newest_within_a_tier(
    db_session, workspace, older_status, newer_status
):
    checksum = "c" * 64
    now = datetime.now(timezone.utc)
    older = _document(
        workspace.id,
        checksum,
        older_status,
        created_at=now,
    )
    newer = _document(
        workspace.id,
        checksum,
        newer_status,
        created_at=now + timedelta(seconds=1),
    )
    db_session.add_all([older, newer])
    db_session.flush()

    found = find_duplicate(db_session, workspace.id, checksum)

    assert found is not None
    assert found.id == newer.id


# ---------------------------------------------------------------------------
# New uploads
# ---------------------------------------------------------------------------

def test_new_upload_creates_one_row_and_one_file_and_queues_it(
    db_session, workspace
):
    uploader = uuid.uuid4()
    storage = RecordingStorage()

    result = _ingest(
        db_session,
        workspace.id,
        storage,
        uploaded_by=uploader,
    )

    assert isinstance(result, UploadResult)
    assert result.duplicate is False
    document = result.document
    assert document.id is not None
    assert document.workspace_id == workspace.id
    assert document.uploaded_by == uploader
    assert document.original_filename == "report.pdf"
    assert document.storage_key == storage_key_for(document.id)
    assert document.mime_type == "application/pdf"
    assert document.size_bytes == len(PDF_BYTES)
    assert document.checksum == hashlib.sha256(PDF_BYTES).hexdigest()
    assert document.status == "queued"
    assert document.processing_attempts == 0
    assert document.error_code is None
    assert document.error_message is None
    assert document.indexed_at is None
    assert storage.read(document.storage_key) == PDF_BYTES
    assert storage.size(document.storage_key) == len(PDF_BYTES)
    assert len(storage.saved) == 1
    saved_key, saved_bytes = storage.saved[0]
    assert saved_key == document.storage_key
    assert saved_bytes == PDF_BYTES

    persisted = db_session.execute(
        select(Document).where(Document.id == document.id)
    ).scalar_one()
    assert persisted.status == "queued"
    assert persisted.storage_key == document.storage_key
    assert len(_documents_for(db_session, workspace.id)) == 1


def test_ingest_upload_returns_existing_duplicate_unchanged(
    db_session, workspace
):
    storage = RecordingStorage()
    first = _ingest(
        db_session,
        workspace.id,
        storage,
        filename="first.pdf",
        uploaded_by=uuid.uuid4(),
    )
    before = _snapshot(first.document)

    duplicate = _ingest(
        db_session,
        workspace.id,
        storage,
        filename="second.pdf",
        uploaded_by=uuid.uuid4(),
    )

    assert duplicate.duplicate is True
    assert duplicate.document.id == before["id"]
    assert _snapshot(duplicate.document) == before
    assert storage.saved == [(before["storage_key"], PDF_BYTES)]
    assert storage.deleted == []
    assert len(_documents_for(db_session, workspace.id)) == 1


def test_allow_duplicate_creates_a_new_document(db_session, workspace):
    storage = RecordingStorage()
    first = _ingest(db_session, workspace.id, storage, filename="first.pdf")

    second = _ingest(
        db_session,
        workspace.id,
        storage,
        filename="second.pdf",
        allow_duplicate=True,
    )

    assert second.duplicate is False
    assert second.document.id != first.document.id
    assert second.document.storage_key != first.document.storage_key
    assert second.document.checksum == first.document.checksum
    assert second.document.status == "queued"
    assert storage.read(first.document.storage_key) == PDF_BYTES
    assert storage.read(second.document.storage_key) == PDF_BYTES
    assert [key for key, _ in storage.saved] == [
        first.document.storage_key,
        second.document.storage_key,
    ]
    assert len(_documents_for(db_session, workspace.id)) == 2


def test_same_checksum_in_another_workspace_is_not_a_duplicate(
    db_session, workspace
):
    other = Workspace(name="other-%s" % uuid.uuid4().hex)
    db_session.add(other)
    db_session.flush()
    storage = RecordingStorage()

    first = _ingest(db_session, workspace.id, storage)
    second = _ingest(db_session, other.id, storage)

    assert first.duplicate is False
    assert second.duplicate is False
    assert first.document.id != second.document.id
    assert first.document.storage_key != second.document.storage_key
    assert storage.read(first.document.storage_key) == PDF_BYTES
    assert storage.read(second.document.storage_key) == PDF_BYTES
    assert len(_documents_for(db_session, workspace.id)) == 1
    assert len(_documents_for(db_session, other.id)) == 1


def test_deleted_document_no_longer_matches(db_session, workspace):
    storage = RecordingStorage()
    first = _ingest(db_session, workspace.id, storage)
    first_id = first.document.id
    first_key = first.document.storage_key
    checksum = first.document.checksum

    assert find_duplicate(db_session, workspace.id, checksum).id == first_id

    db_session.delete(first.document)
    db_session.commit()
    assert storage.delete(first_key) is True

    assert find_duplicate(db_session, workspace.id, checksum) is None

    second = _ingest(db_session, workspace.id, storage)

    assert second.duplicate is False
    assert second.document.id != first_id
    assert second.document.storage_key != first_key
    assert len(_documents_for(db_session, workspace.id)) == 1
    assert storage.exists(first_key) is False
    assert storage.exists(second.document.storage_key) is True
    assert storage.read(second.document.storage_key) == PDF_BYTES


# ---------------------------------------------------------------------------
# Validation and bounded reads
# ---------------------------------------------------------------------------

def test_oversized_upload_is_read_with_a_bounded_size(
    db_session, workspace
):
    storage = RecordingStorage()
    oversized = PDF_BYTES + b"x"
    stream = RecordingStream(oversized)

    with pytest.raises(InvalidUploadError):
        _ingest(
            db_session,
            workspace.id,
            storage,
            stream=stream,
            max_upload_bytes=len(PDF_BYTES),
        )

    assert stream.read_sizes == [len(PDF_BYTES) + 1]
    assert storage.saved == []
    assert len(_documents_for(db_session, workspace.id)) == 0


def test_upload_at_exact_limit_is_accepted(db_session, workspace, memory_storage):
    result = _ingest(
        db_session,
        workspace.id,
        memory_storage,
        max_upload_bytes=len(PDF_BYTES),
    )

    assert result.duplicate is False
    assert result.document.size_bytes == len(PDF_BYTES)
    assert memory_storage.read(result.document.storage_key) == PDF_BYTES


@pytest.mark.parametrize(
    ("content", "filename"),
    [
        (b"", "empty.pdf"),
        (b"not a PDF", "not-a-pdf.pdf"),
        (PDF_BYTES, "report.txt"),
    ],
)
def test_invalid_upload_leaves_no_file_or_row(
    db_session, workspace, content, filename
):
    storage = RecordingStorage()

    with pytest.raises(InvalidUploadError):
        _ingest(
            db_session,
            workspace.id,
            storage,
            content=content,
            filename=filename,
        )

    assert storage.saved == []
    assert len(_documents_for(db_session, workspace.id)) == 0


def test_filename_is_normalised_before_persistence(
    db_session, workspace, memory_storage
):
    result = _ingest(
        db_session,
        workspace.id,
        memory_storage,
        filename="../../etc/passwd.pdf",
    )

    assert result.document.original_filename == "passwd.pdf"
    assert result.document.storage_key == storage_key_for(result.document.id)
    assert "passwd" not in result.document.storage_key


# ---------------------------------------------------------------------------
# Failure and compensation
# ---------------------------------------------------------------------------

def test_storage_failure_does_not_create_document(db_session, workspace):
    storage = FailingStorage()

    with pytest.raises(StorageUnavailableError):
        _ingest(db_session, workspace.id, storage)

    assert storage.save_calls == 1
    assert len(_documents_for(db_session, workspace.id)) == 0


def test_database_failure_removes_the_stored_file(db_session):
    storage = RecordingStorage()
    missing_workspace_id = uuid.uuid4()

    with pytest.raises(IntegrityError):
        _ingest(db_session, missing_workspace_id, storage)

    assert len(storage.saved) == 1
    key = storage.saved[0][0]
    document_id = uuid.UUID(key.split("/")[1])
    assert storage.deleted == [key]
    assert storage.exists(key) is False
    assert (
        db_session.execute(
            select(Document).where(Document.id == document_id)
        ).scalar_one_or_none()
        is None
    )


def test_compensation_delete_failure_does_not_mask_database_error(db_session):
    storage = DeleteFailingStorage()
    missing_workspace_id = uuid.uuid4()

    with pytest.raises(IntegrityError):
        _ingest(db_session, missing_workspace_id, storage)

    assert len(storage.saved) == 1
    key = storage.saved[0][0]
    assert storage.exists(key) is True
