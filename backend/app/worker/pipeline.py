"""The document claim: the only entry to ``processing``.

The contract is defined in ``docs/phase-1/05-job-contract.md``. A task
payload carries only ``document_id`` (D-017), so the claim first resolves
the document's ``workspace_id`` and then repeats it in the UPDATE's WHERE
clause. That keeps the state transition scoped exactly like the contract
and gives the new ``processing_runs`` row its workspace snapshot.

Exactly one worker can win a claim:

* the UPDATE is conditional on ``status IN ('queued', 'retrying')`` and
  on the attempt budget;
* PostgreSQL locks the row for the duration of the transaction, so a
  concurrent UPDATE waits and then re-evaluates the predicate against the
  committed ``processing`` row, matching zero rows.

Losers return ``None`` with no side effects. The function owns its
transaction and commits before returning; file I/O happens later, outside
this transaction, so a slow storage backend never holds a document row
lock.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.models import Document, ProcessingRun

logger = logging.getLogger("axiom.worker.pipeline")

__all__ = ["ClaimedDocument", "claim_document"]


@dataclass(frozen=True)
class ClaimedDocument:
    """The immutable facts a claimed worker needs for the rest of the job.

    ``attempt_no`` is the value returned by the UPDATE's RETURNING clause,
    i.e. the attempt this worker just claimed. It is the number persisted
    on the corresponding ``processing_runs`` row.
    """

    id: uuid.UUID
    workspace_id: uuid.UUID
    attempt_no: int
    storage_key: str
    checksum: str
    size_bytes: int
    mime_type: str
    original_filename: str


def claim_document(
    session: Session,
    document_id: uuid.UUID,
    max_attempts: int,
) -> ClaimedDocument | None:
    """Atomically move one document to ``processing`` and open a run row.

    Returns ``None`` when the document does not exist, is not in a
    claimable state, or has already used its attempt budget. Every
    no-op path rolls back, so it leaves no transaction or lock behind.

    The caller must not have unrelated pending work in ``session``: this
    function commits or rolls back its own unit of work.
    """
    workspace_id = session.execute(
        select(Document.workspace_id).where(Document.id == document_id)
    ).scalar_one_or_none()

    if workspace_id is None:
        session.rollback()
        logger.debug(
            "Document %s was not claimed: no such document.", document_id
        )
        return None

    row = session.execute(
        update(Document)
        .where(
            Document.id == document_id,
            Document.workspace_id == workspace_id,
            Document.status.in_(("queued", "retrying")),
            Document.processing_attempts < max_attempts,
        )
        .values(
            status="processing",
            processing_attempts=Document.processing_attempts + 1,
            updated_at=func.now(),
            error_code=None,
            error_message=None,
            retryable=False,
        )
        .returning(
            Document.id,
            Document.workspace_id,
            Document.processing_attempts,
            Document.storage_key,
            Document.checksum,
            Document.size_bytes,
            Document.mime_type,
            Document.original_filename,
        )
        # The row was not loaded as an ORM entity; RETURNING already gives
        # this function every value it needs, so no identity-map sync is
        # required (or wanted).
        .execution_options(synchronize_session=False)
    ).one_or_none()

    if row is None:
        session.rollback()
        logger.debug(
            "Document %s was not claimed: not queued/retrying, or at the "
            "attempt cap.",
            document_id,
        )
        return None

    claimed = ClaimedDocument(
        id=row.id,
        workspace_id=row.workspace_id,
        attempt_no=row.processing_attempts,
        storage_key=row.storage_key,
        checksum=row.checksum,
        size_bytes=row.size_bytes,
        mime_type=row.mime_type,
        original_filename=row.original_filename,
    )

    try:
        session.add(
            ProcessingRun(
                document_id=claimed.id,
                workspace_id=claimed.workspace_id,
                attempt_no=claimed.attempt_no,
                stage="claimed",
                started_at=func.now(),
            )
        )
        session.commit()
    except Exception:
        # A failed run insert must leave the document claimable again.
        # commit() has already ended the transaction; rollback clears the
        # failed state so the caller can close or reuse the session.
        session.rollback()
        raise

    logger.info(
        "Document %s claimed for workspace %s (attempt %d/%d).",
        claimed.id,
        claimed.workspace_id,
        claimed.attempt_no,
        max_attempts,
    )
    return claimed
