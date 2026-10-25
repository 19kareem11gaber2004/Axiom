"""Tests for the Phase 4 claim and processing-run lifecycle.

The claim is the only entry to ``processing`` and must be safe under
duplicate Celery delivery. These tests exercise it through a real
PostgreSQL session: the conditional UPDATE, the attempt cap, the run row,
and the rollback that keeps the document claimable when the run insert
fails.

The shared ``db_session`` fixture joins an outer transaction through a
savepoint. ``claim_document`` owns its transaction and may roll it back,
so each test commits its preconditions first; the outer transaction still
rolls everything back at teardown.
"""

from __future__ import annotations

import hashlib
import logging
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models import Document, ProcessingRun, User, Workspace
from app.storage.keys import storage_key_for
from app.worker.pipeline import ClaimedDocument, claim_document

PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"


def _runs(session, document_id: uuid.UUID) -> list[ProcessingRun]:
    return list(
        session.execute(
            select(ProcessingRun)
            .where(ProcessingRun.document_id == document_id)
            .order_by(ProcessingRun.attempt_no)
        ).scalars()
    )


def _make_document(
    db_session,
    *,
    status: str = "queued",
    processing_attempts: int = 0,
    retryable: bool = False,
    error_code: str | None = None,
    error_message: str | None = None,
) -> Document:
    """Create the committed precondition for one claim.

    The commit is deliberate. ``claim_document`` rolls back its own unit
    of work on a no-op, and a rollback inside the savepoint fixture would
    otherwise erase the document this test just created.
    """
    workspace = Workspace(name="ws-%s" % uuid.uuid4().hex)
    user = User(
        email="worker-%s@example.com" % uuid.uuid4().hex,
        password_hash="not-used-by-the-worker",
    )
    db_session.add_all([workspace, user])
    db_session.flush()

    document_id = uuid.uuid4()
    document = Document(
        id=document_id,
        workspace_id=workspace.id,
        uploaded_by=user.id,
        original_filename="report.pdf",
        storage_key=storage_key_for(document_id),
        mime_type="application/pdf",
        size_bytes=len(PDF_BYTES),
        checksum=hashlib.sha256(PDF_BYTES).hexdigest(),
        status=status,
        processing_attempts=processing_attempts,
        error_code=error_code,
        error_message=error_message,
        retryable=retryable,
    )
    db_session.add(document)
    db_session.commit()
    return document


# ---------------------------------------------------------------------------
# The winning claim
# ---------------------------------------------------------------------------

def test_claim_moves_a_queued_document_to_processing(db_session):
    document = _make_document(
        db_session,
        retryable=True,
        error_code="OLD_CODE",
        error_message="a previous failure",
    )

    claimed = claim_document(db_session, document.id, max_attempts=5)

    assert isinstance(claimed, ClaimedDocument)
    assert claimed.id == document.id
    assert claimed.workspace_id == document.workspace_id
    assert claimed.attempt_no == 1
    assert claimed.storage_key == document.storage_key
    assert claimed.checksum == document.checksum
    assert claimed.size_bytes == document.size_bytes
    assert claimed.mime_type == document.mime_type
    assert claimed.original_filename == document.original_filename

    db_session.refresh(document)
    assert document.status == "processing"
    assert document.processing_attempts == 1
    assert document.error_code is None
    assert document.error_message is None
    assert document.retryable is False

    runs = _runs(db_session, document.id)
    assert len(runs) == 1
    assert runs[0].workspace_id == document.workspace_id
    assert runs[0].attempt_no == 1
    assert runs[0].stage == "claimed"
    assert runs[0].started_at is not None
    assert runs[0].finished_at is None


def test_a_retrying_document_is_claimable(db_session):
    document = _make_document(
        db_session,
        status="retrying",
        processing_attempts=2,
        retryable=True,
        error_code="STORAGE_UNAVAILABLE",
        error_message="storage was briefly unavailable",
    )

    claimed = claim_document(db_session, document.id, max_attempts=5)

    assert claimed is not None
    assert claimed.attempt_no == 3

    db_session.refresh(document)
    assert document.status == "processing"
    assert document.processing_attempts == 3
    assert document.error_code is None
    assert document.retryable is False

    assert [run.attempt_no for run in _runs(db_session, document.id)] == [3]


def test_a_duplicate_delivery_does_not_claim_again(db_session):
    document = _make_document(db_session)

    first = claim_document(db_session, document.id, max_attempts=5)
    second = claim_document(db_session, document.id, max_attempts=5)

    assert first is not None
    assert second is None

    db_session.refresh(document)
    assert document.status == "processing"
    assert document.processing_attempts == 1

    runs = _runs(db_session, document.id)
    assert len(runs) == 1
    assert runs[0].attempt_no == 1


# ---------------------------------------------------------------------------
# No-op paths
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status", ["uploaded", "processing", "indexed", "failed"]
)
def test_documents_outside_the_claimable_states_are_not_claimed(
    db_session, status
):
    document = _make_document(
        db_session,
        status=status,
        processing_attempts=2,
        retryable=True,
        error_code="PREVIOUS",
        error_message="previous state",
    )

    assert claim_document(db_session, document.id, max_attempts=5) is None

    db_session.refresh(document)
    assert document.status == status
    assert document.processing_attempts == 2
    assert document.error_code == "PREVIOUS"
    assert document.retryable is True
    assert _runs(db_session, document.id) == []


def test_a_deleted_document_is_not_claimed(db_session):
    document = _make_document(db_session)
    document_id = document.id

    db_session.delete(document)
    db_session.commit()

    assert claim_document(db_session, document_id, max_attempts=5) is None
    assert _runs(db_session, document_id) == []


def test_a_document_at_the_attempt_cap_is_not_claimed(db_session, caplog):
    document = _make_document(db_session, processing_attempts=5)

    with caplog.at_level(logging.DEBUG, logger="axiom.worker.pipeline"):
        claimed = claim_document(db_session, document.id, max_attempts=5)

    assert claimed is None
    db_session.refresh(document)
    assert document.status == "queued"
    assert document.processing_attempts == 5
    assert _runs(db_session, document.id) == []

    debug_messages = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.DEBUG
    ]
    assert any(str(document.id) in message for message in debug_messages)


def test_the_last_attempt_below_the_cap_is_still_claimable(db_session):
    """The cap is the number of claims, not a zero-based counter."""
    document = _make_document(db_session, processing_attempts=4)

    claimed = claim_document(db_session, document.id, max_attempts=5)

    assert claimed is not None
    assert claimed.attempt_no == 5

    db_session.refresh(document)
    assert document.status == "processing"
    assert document.processing_attempts == 5
    assert [run.attempt_no for run in _runs(db_session, document.id)] == [5]


# ---------------------------------------------------------------------------
# One transaction: the claim and its run row
# ---------------------------------------------------------------------------

def test_a_failed_run_insert_rolls_the_claim_back(db_session):
    """The UPDATE and the run INSERT must be all-or-nothing.

    A pre-existing run with the same attempt number makes the INSERT fail
    its unique constraint. If the claim were committed separately, the
    document would be stuck in ``processing`` with no corresponding run.
    """
    document = _make_document(db_session)
    db_session.add(
        ProcessingRun(
            document_id=document.id,
            workspace_id=document.workspace_id,
            attempt_no=1,
            stage="indexed",
        )
    )
    db_session.commit()

    with pytest.raises(IntegrityError):
        claim_document(db_session, document.id, max_attempts=5)

    db_session.refresh(document)
    assert document.status == "queued"
    assert document.processing_attempts == 0

    runs = _runs(db_session, document.id)
    assert len(runs) == 1
    assert runs[0].stage == "indexed"
