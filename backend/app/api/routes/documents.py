"""Document routes: upload, list, inspect, download, retry, delete.

Workspace isolation is enforced in the SQL, never in Python after the
fact: every query filters on the caller's workspace, so another
workspace's rows are never even returned to the application.

A document belonging to a different workspace is reported as 404, not
403. A 403 would confirm that the id exists; a 404 gives an attacker
nothing to work with.
"""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, Response, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.errors import ConflictError, NotFoundError, ValidationError
from app.api.schemas import (
    DocumentListItem,
    DocumentListResponse,
    DocumentRead,
    DocumentUploadResponse,
)
from app.auth.dependencies import (
    CurrentMembership,
    ensure_role,
    get_current_membership,
    require_role,
)
from app.config import settings
from app.db.models import Document
from app.db.session import get_db
from app.ingestion.validation import InvalidUploadError, normalise_filename
from app.ingestion.validation import validate_pdf_upload
from app.storage import uploads

logger = logging.getLogger("axiom.documents")

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])

# Roles allowed to act on a document, per docs/phase-1/03-erd-api.md §5.
UPLOAD_ROLE = ("member",)
READ_ROLE = ("viewer",)
DELETE_ROLE = ("admin",)

DEFAULT_LIMIT = 20
MAX_LIMIT = 100

# Deleting a document is blocked while a worker owns it.
UNDELETABLE_STATUSES = ("processing", "retrying")


def _to_read(document: Document) -> DocumentRead:
    return DocumentRead(
        id=document.id,
        workspace_id=document.workspace_id,
        uploaded_by=document.uploaded_by,
        original_filename=document.original_filename,
        storage_key=document.storage_key,
        mime_type=document.mime_type,
        size_bytes=document.size_bytes,
        checksum=document.checksum,
        status=document.status,
        processing_attempts=document.processing_attempts,
        error_code=document.error_code,
        error_message=document.error_message,
        created_at=document.created_at,
        updated_at=document.updated_at,
        indexed_at=document.indexed_at,
    )


def _to_list_item(document: Document) -> DocumentListItem:
    return DocumentListItem(
        id=document.id,
        original_filename=document.original_filename,
        status=document.status,
        processing_attempts=document.processing_attempts,
        error_code=document.error_code,
        created_at=document.created_at,
    )


def _load_document(
    db: Session,
    document_id: uuid.UUID,
    membership: CurrentMembership,
) -> Document:
    """Load a document that belongs to the caller's workspace.

    Raises 404 when the document does not exist *or* belongs to another
    workspace - the caller cannot tell the difference.
    """
    document = db.execute(
        select(Document).where(
            Document.id == document_id,
            Document.workspace_id == membership.workspace_id,
        )
    ).scalar_one_or_none()

    if document is None:
        raise NotFoundError("Document not found.")
    return document


@router.post(
    "",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def upload_document(
    file: UploadFile = File(...),
    membership: CurrentMembership = Depends(require_role(*UPLOAD_ROLE)),
    db: Session = Depends(get_db),
):
    """Accept a PDF for background processing.

    The 202 is returned only after the file has been written and the
    document row has been committed. Processing and indexing happen later
    in the worker (Phase 4); until then the document stays "queued".

    The response is 202 Accepted rather than 200 Created because the
    document is not searchable yet.
    """
    filename = normalise_filename(file.filename)

    # Reject an oversized file before reading it into memory.
    if file.size is not None and file.size > settings.max_upload_bytes:
        raise ValidationError(
            "The file is larger than the %d byte limit." % settings.max_upload_bytes
        )

    content = file.file.read()

    try:
        validate_pdf_upload(filename, content, settings.max_upload_bytes)
    except InvalidUploadError as exc:
        raise ValidationError(str(exc)) from exc

    document = Document(
        workspace_id=membership.workspace_id,
        uploaded_by=membership.user.id,
        original_filename=filename,
        storage_key="",  # filled in below, once the id exists
        mime_type="application/pdf",
        size_bytes=len(content),
        checksum=uploads.checksum_of(content),
        status="uploaded",
    )
    db.add(document)
    db.flush()  # assigns document.id

    document.storage_key = uploads.storage_key_for(document.id)

    try:
        uploads.save_upload(document.id, content)
    except OSError as exc:
        db.rollback()
        logger.error("Failed to store upload for document %s", document.id)
        raise ValidationError("The file could not be stored.") from exc

    # The row is only promoted to "queued" once the file is safely on
    # disk, so a queued document always has its file available.
    document.status = "queued"
    document.updated_at = func.now()
    db.commit()
    db.refresh(document)

    logger.info(
        "Document %s uploaded to workspace %s (%d bytes)",
        document.id,
        document.workspace_id,
        document.size_bytes,
    )

    return DocumentUploadResponse(document_id=document.id, status=document.status)


@router.get("", response_model=DocumentListResponse)
def list_documents(
    status_filter: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    membership: CurrentMembership = Depends(require_role(*READ_ROLE)),
    db: Session = Depends(get_db),
):
    """List the caller's workspace documents.

    Ordering is (created_at, id) so that pagination stays stable while
    new documents arrive.
    """
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)

    filters = [Document.workspace_id == membership.workspace_id]
    if status_filter:
        filters.append(Document.status == status_filter)
    if q:
        filters.append(Document.original_filename.ilike("%%%s%%" % q))

    total = db.execute(
        select(func.count()).select_from(Document).where(*filters)
    ).scalar_one()

    documents = db.execute(
        select(Document)
        .where(*filters)
        .order_by(Document.created_at, Document.id)
        .limit(limit)
        .offset(offset)
    ).scalars().all()

    return DocumentListResponse(
        items=[_to_list_item(d) for d in documents],
        total=total,
    )


@router.get("/{document_id}", response_model=DocumentRead)
def get_document(
    document_id: uuid.UUID,
    membership: CurrentMembership = Depends(get_current_membership),
    db: Session = Depends(get_db),
):
    """Return a document's metadata and processing status."""
    document = _load_document(db, document_id, membership)
    ensure_role(membership, *READ_ROLE)
    return _to_read(document)


@router.get("/{document_id}/file")
def download_document(
    document_id: uuid.UUID,
    membership: CurrentMembership = Depends(get_current_membership),
    db: Session = Depends(get_db),
):
    """Download the original file.

    This is the only authorized path to the bytes. The frontend never
    touches storage directly, and no local path is ever exposed.
    """
    document = _load_document(db, document_id, membership)
    ensure_role(membership, *READ_ROLE)

    if document.status == "uploaded":
        raise ConflictError("The document is not ready for download yet.")

    try:
        content = uploads.read_upload(document.storage_key)
    except (OSError, ValueError) as exc:
        logger.error("Stored file for document %s is unreadable", document.id)
        raise ConflictError("The stored file could not be read.") from exc

    return Response(
        content=content,
        media_type=document.mime_type or "application/pdf",
        headers={
            "Content-Disposition": 'inline; filename="%s"' % document.original_filename,
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/{document_id}/retry", response_model=DocumentUploadResponse)
def retry_document(
    document_id: uuid.UUID,
    membership: CurrentMembership = Depends(get_current_membership),
    db: Session = Depends(get_db),
):
    """Re-queue a failed document for processing.

    Only a failed document can be retried. The distinction between a
    permanent and a transient failure arrives with the worker's error
    taxonomy in Phase 4.
    """
    document = _load_document(db, document_id, membership)
    ensure_role(membership, *UPLOAD_ROLE)

    if document.status != "failed":
        raise ConflictError(
            "Only a failed document can be retried (current status: %s)."
            % document.status
        )

    document.status = "queued"
    document.error_code = None
    document.error_message = None
    document.processing_attempts += 1
    document.updated_at = func.now()
    db.commit()
    db.refresh(document)

    logger.info("Document %s re-queued for processing", document.id)
    return DocumentUploadResponse(document_id=document.id, status=document.status)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    document_id: uuid.UUID,
    membership: CurrentMembership = Depends(get_current_membership),
    db: Session = Depends(get_db),
):
    """Delete a document, its file, and its derived data.

    Conversation history is not affected: citations keep their own
    snapshot of the passage text, so past answers stay readable.
    """
    document = _load_document(db, document_id, membership)
    ensure_role(membership, *DELETE_ROLE)

    if document.status in UNDELETABLE_STATUSES:
        raise ConflictError(
            "A document that is being processed cannot be deleted."
        )

    storage_key = document.storage_key

    # Remove the row first. chunks and processing_runs are removed by the
    # ON DELETE CASCADE constraints; citations keep their snapshots.
    db.delete(document)
    db.commit()

    removed = uploads.delete_upload(storage_key)
    logger.info(
        "Document %s deleted by user %s (file removed: %s)",
        document.id,
        membership.user.id,
        removed,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
