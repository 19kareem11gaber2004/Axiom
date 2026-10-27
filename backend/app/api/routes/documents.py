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
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api.errors import (
    ConflictError,
    DependencyUnavailableError,
    NotFoundError,
    ValidationError,
)
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
from app.db.models import Document
from app.db.session import get_db
from app.ingestion.service import ingest_upload
from app.ingestion.validation import InvalidUploadError
from app.storage import (
    FileStorage,
    StorageNotFoundError,
    StorageUnavailableError,
    get_storage,
)
from app.worker.publisher import TaskPublisher, get_task_publisher

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
        retryable=document.retryable,
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
        retryable=document.retryable,
        created_at=document.created_at,
    )


def _publish_document(
    publisher: TaskPublisher,
    document_id: uuid.UUID,
    status: str,
) -> None:
    """Publish one task and keep a broker failure out of the response.

    The database transition is already committed. A failed publish is
    recoverable by the sweeper, so it must not turn a successful upload or
    retry into an API error.
    """
    if publisher.enqueue_document(document_id, countdown=0):
        return
    logger.warning(
        "document.enqueue document_id=%s status=%s published=false",
        document_id,
        status,
    )


def _load_document(
    db: Session,
    document_id: uuid.UUID,
    membership: CurrentMembership,
    *,
    for_update: bool = False,
) -> Document:
    """Load a document that belongs to the caller's workspace.

    Raises 404 when the document does not exist *or* belongs to another
    workspace - the caller cannot tell the difference.

    ``for_update=True`` takes a row lock with the SELECT. The delete route
    uses it so a concurrent worker claim must wait and then observe either
    a deleted row or a committed status it must respect.
    """
    statement = select(Document).where(
        Document.id == document_id,
        Document.workspace_id == membership.workspace_id,
    )
    if for_update:
        statement = statement.with_for_update()

    document = db.execute(statement).scalar_one_or_none()

    if document is None:
        raise NotFoundError("Document not found.")
    return document


@router.post(
    "",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_200_OK: {
            "model": DocumentUploadResponse,
            "description": (
                "Duplicate upload; the existing document is returned."
            ),
        },
        status.HTTP_202_ACCEPTED: {
            "model": DocumentUploadResponse,
            "description": (
                "New document stored and queued for processing."
            ),
        },
    },
)
def upload_document(
    response: Response,
    allow_duplicate: bool = False,
    file: UploadFile = File(...),
    membership: CurrentMembership = Depends(require_role(*UPLOAD_ROLE)),
    db: Session = Depends(get_db),
    storage: FileStorage = Depends(get_storage),
    publisher: TaskPublisher = Depends(get_task_publisher),
):
    """Accept a PDF for background processing.

    A new document is stored and committed as "queued", then published and
    answered with 202. If the workspace already contains the same checksum,
    the existing document is returned with 200 and ``duplicate=true`` and
    no task is published. Passing ``allow_duplicate=true`` creates a second
    document instead, which is published normally.

    The ingestion service owns validation, deduplication, storage and the
    single commit. This route maps its domain errors onto HTTP and starts
    background processing after that commit.
    """
    try:
        result = ingest_upload(
            db,
            membership.workspace_id,
            membership.user.id,
            file.filename,
            file.file,
            allow_duplicate=allow_duplicate,
            storage=storage,
        )
    except InvalidUploadError as exc:
        raise ValidationError(str(exc)) from exc
    except StorageUnavailableError as exc:
        logger.error(
            "Storage unavailable while uploading to workspace %s",
            membership.workspace_id,
            exc_info=True,
        )
        raise DependencyUnavailableError(
            "Storage is temporarily unavailable; the upload was not accepted."
        ) from exc

    if not result.duplicate:
        _publish_document(publisher, result.document.id, result.document.status)

    response.status_code = (
        status.HTTP_200_OK if result.duplicate else status.HTTP_202_ACCEPTED
    )
    return DocumentUploadResponse(
        document_id=result.document.id,
        status=result.document.status,
        duplicate=result.duplicate,
    )


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
    storage: FileStorage = Depends(get_storage),
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
        content = storage.read(document.storage_key)
    except StorageNotFoundError as exc:
        logger.error("Stored file for document %s is missing", document.id)
        raise ConflictError("The stored file is missing.") from exc
    except StorageUnavailableError as exc:
        logger.error(
            "Storage unavailable while downloading document %s",
            document.id,
            exc_info=True,
        )
        raise DependencyUnavailableError(
            "Storage is temporarily unavailable; the file could not be read."
        ) from exc

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
    publisher: TaskPublisher = Depends(get_task_publisher),
):
    """Re-queue a failed, retryable document for processing.

    The state change is one conditional UPDATE, so two concurrent retries
    cannot both succeed: PostgreSQL serializes them on the row and the
    loser re-evaluates the predicate against the committed ``queued`` row.
    """
    _load_document(db, document_id, membership)
    ensure_role(membership, *UPLOAD_ROLE)

    row = db.execute(
        update(Document)
        .where(
            Document.id == document_id,
            Document.workspace_id == membership.workspace_id,
            Document.status == "failed",
            Document.retryable.is_(True),
        )
        .values(
            status="queued",
            error_code=None,
            error_message=None,
            retryable=False,
            updated_at=func.now(),
        )
        .returning(Document.id)
        .execution_options(synchronize_session=False)
    ).scalar_one_or_none()

    if row is None:
        db.rollback()
        raise ConflictError(
            "The document is not failed or is not retryable, so it cannot "
            "be retried."
        )

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise

    _publish_document(publisher, row, "queued")

    logger.info("Document %s re-queued for processing", row)
    return DocumentUploadResponse(
        document_id=row,
        status="queued",
        duplicate=False,
    )


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    document_id: uuid.UUID,
    membership: CurrentMembership = Depends(get_current_membership),
    db: Session = Depends(get_db),
    storage: FileStorage = Depends(get_storage),
):
    """Delete a document, its file, and its derived data.

    Conversation history is not affected: citations keep their own
    snapshot of the passage text, so past answers stay readable.

    The storage object is removed inside the database transaction's
    window: the row is locked, deleted, and flushed first, then the object
    is removed, then the transaction commits. A concurrent worker claim
    must wait on the row lock and then either finds no document or sees
    the committed deletion. A storage outage rolls the row deletion back.
    The two systems cannot be perfectly atomic, so if the file is removed
    and the commit then fails, the row remains and a retry safely completes
    the delete (a missing object is idempotent).
    """
    document = _load_document(db, document_id, membership, for_update=True)
    ensure_role(membership, *DELETE_ROLE)

    if document.status in UNDELETABLE_STATUSES:
        db.rollback()
        raise ConflictError(
            "A document that is being processed cannot be deleted."
        )

    document_id = document.id
    storage_key = document.storage_key

    try:
        # chunks and processing_runs are removed by the ON DELETE CASCADE
        # constraints; citations keep their snapshots.
        db.delete(document)
        db.flush()
        removed = storage.delete(storage_key)
        db.commit()
    except StorageUnavailableError as exc:
        db.rollback()
        logger.error(
            "Storage unavailable while deleting document %s",
            document_id,
            exc_info=True,
        )
        raise DependencyUnavailableError(
            "Storage is temporarily unavailable; the document was not deleted."
        ) from exc
    except Exception:
        db.rollback()
        logger.exception("Failed to delete document %s", document_id)
        raise

    logger.info(
        "Document %s deleted by user %s (file removed: %s)",
        document_id,
        membership.user.id,
        removed,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
