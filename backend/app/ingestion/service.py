"""Upload ingestion service: validate, deduplicate, store, and queue a document.

This module is deliberately HTTP-agnostic. It raises domain exceptions
(``InvalidUploadError`` and the storage errors); the API layer maps them
onto the structured error envelope in ``app.api.errors``.

The upload contract implemented here is fixed by the Phase 3 decisions:

* Deduplication is workspace-scoped SHA-256 and advisory (D-006). It is
  not a unique constraint; two concurrent identical uploads may both
  create a document.
* A duplicate returns the existing document unchanged. The caller decides
  whether that becomes ``200`` or, with ``allow_duplicate=True``, a new
  ``202`` document.
* The file is made durable before the row is committed, and the row is
  inserted directly as ``queued`` in one commit (D-009). The ``uploaded``
  state is reserved for the Phase 4 enqueue/sweeper path.
* If the database write fails after the file was saved, the service rolls
  back and best-effort deletes the newly stored object, so a failed
  request does not normally leave an orphaned file.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from typing import BinaryIO

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Document
from app.ingestion.validation import normalise_filename, validate_pdf_upload
from app.storage import FileStorage, StorageError, get_storage
from app.storage.keys import storage_key_for

logger = logging.getLogger("axiom.ingestion")

__all__ = ["UploadResult", "find_duplicate", "ingest_upload"]

PDF_MIME_TYPE = "application/pdf"

# Every matching row is eligible. The CASE expression only controls which
# healthy duplicate is returned when a checksum has several rows:
# indexed is best, an in-flight document is next, then uploaded, then
# failed. Rows are newest-first within a tier.
_DUPLICATE_STATUS_PRIORITY = case(
    (Document.status == "indexed", 0),
    (Document.status.in_(("queued", "processing", "retrying")), 1),
    (Document.status == "uploaded", 2),
    (Document.status == "failed", 3),
    else_=4,
)


@dataclass(frozen=True)
class UploadResult:
    """The outcome of one upload request.

    ``duplicate`` is True only when the caller asked for the default
    behaviour and an existing document with the same checksum was
    returned. A forced re-upload (``allow_duplicate=True``) always
    creates a new document and therefore reports ``duplicate=False``.
    """

    document: Document
    duplicate: bool


def find_duplicate(
    db: Session,
    workspace_id: uuid.UUID,
    checksum: str,
) -> Document | None:
    """Return the best existing document for a workspace and checksum.

    The WHERE clause matches ``ix_documents_ws_checksum`` exactly. A
    status filter is deliberately absent: deduplication considers every
    status, including ``failed``, so a retry of the same bytes still
    returns the original document rather than creating a second one.
    """
    statement = (
        select(Document)
        .where(
            Document.workspace_id == workspace_id,
            Document.checksum == checksum,
        )
        .order_by(
            _DUPLICATE_STATUS_PRIORITY,
            Document.created_at.desc(),
            Document.id.desc(),
        )
        .limit(1)
    )
    return db.execute(statement).scalar_one_or_none()


def ingest_upload(
    db: Session,
    workspace_id: uuid.UUID,
    uploaded_by: uuid.UUID,
    filename: str | None,
    stream: BinaryIO,
    *,
    allow_duplicate: bool = False,
    storage: FileStorage | None = None,
    max_upload_bytes: int | None = None,
) -> UploadResult:
    """Validate, deduplicate, store, and queue one PDF upload.

    ``stream`` must be a binary stream positioned at the start of the
    file, as ``UploadFile.file`` is. It is read at most
    ``max_upload_bytes + 1`` bytes: the extra byte is what reveals an
    oversized upload without reading the whole body into memory.

    The service owns the existing filename normalization and PDF
    validation because it is the component that reads the stream.
    Validation happens before any storage or database write.

    The generated document id is used for the storage key, so the file
    can be written before the row exists. A successful call commits
    exactly once. A failure after the file is written triggers a
    rollback and a best-effort delete of that object.
    """
    limit = (
        max_upload_bytes
        if max_upload_bytes is not None
        else settings.max_upload_bytes
    )

    safe_name = normalise_filename(filename)
    content = stream.read(limit + 1)

    validate_pdf_upload(safe_name, content, limit)
    checksum = hashlib.sha256(content).hexdigest()

    if not allow_duplicate:
        existing = find_duplicate(db, workspace_id, checksum)
        if existing is not None:
            logger.info(
                "Duplicate upload in workspace %s matched document %s (status=%s)",
                workspace_id,
                existing.id,
                existing.status,
            )
            return UploadResult(document=existing, duplicate=True)

    document_id = uuid.uuid4()
    storage_key = storage_key_for(document_id)
    adapter = storage if storage is not None else get_storage()

    adapter.save(storage_key, content)

    try:
        document = Document(
            id=document_id,
            workspace_id=workspace_id,
            uploaded_by=uploaded_by,
            original_filename=safe_name,
            storage_key=storage_key,
            mime_type=PDF_MIME_TYPE,
            size_bytes=len(content),
            checksum=checksum,
            status="queued",
        )
        db.add(document)
        db.commit()
    except Exception:
        _rollback_quietly(db, document_id)
        _discard_stored_upload_quietly(adapter, storage_key, document_id)
        raise

    # The row and file are durable at this point. A refresh failure must
    # not trigger compensation; it is outside the try/except above.
    db.refresh(document)

    logger.info(
        "Document %s ingested into workspace %s (%d bytes)",
        document.id,
        document.workspace_id,
        document.size_bytes,
    )
    return UploadResult(document=document, duplicate=False)


def _rollback_quietly(db: Session, document_id: uuid.UUID) -> None:
    """Roll back before compensation without hiding the original error."""
    try:
        db.rollback()
    except Exception:  # noqa: BLE001 - cleanup must never mask the DB error
        logger.exception(
            "Failed to roll back the session for document %s.", document_id
        )


def _discard_stored_upload_quietly(
    storage: FileStorage,
    storage_key: str,
    document_id: uuid.UUID,
) -> None:
    """Best-effort delete of a file whose database write failed.

    A cleanup failure is logged and swallowed: the original database
    exception, raised by the caller, remains the error the request sees.
    """
    try:
        removed = storage.delete(storage_key)
    except StorageError:
        logger.exception(
            "Failed to remove stored upload %s for document %s after a "
            "database failure.",
            storage_key,
            document_id,
        )
        return
    except Exception:  # noqa: BLE001 - cleanup must never mask the DB error
        logger.exception(
            "Unexpected error removing stored upload %s for document %s.",
            storage_key,
            document_id,
        )
        return

    if not removed:
        logger.warning(
            "Stored upload %s for document %s was already absent during "
            "compensation.",
            storage_key,
            document_id,
        )
