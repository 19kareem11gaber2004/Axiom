"""Concurrency test for the document claim.

The shared ``db_session`` fixture wraps a test in one outer transaction,
which cannot exercise two simultaneous PostgreSQL transactions. This file
therefore uses a fixture that creates and really commits a workspace,
user, and document, then removes them in ``finally``. Two worker threads
open independent sessions and race the claim.

The expected result is a single winner, ``processing_attempts = 1``, and
one ``processing_runs`` row. The loser must observe zero rows from its
UPDATE after PostgreSQL releases the row lock, not an error.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from sqlalchemy import delete, select

from app.db.models import Document, ProcessingRun, User, Workspace
from app.db.session import SessionLocal, psycopg_dsn
from app.storage.keys import storage_key_for
from app.worker.pipeline import ClaimedDocument, claim_document

PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"


def _require_database() -> None:
    """Skip like ``db_session`` does when PostgreSQL is unreachable."""
    try:
        probe = psycopg.connect(psycopg_dsn(), connect_timeout=3)
        probe.close()
    except Exception as exc:
        pytest.skip("PostgreSQL not available: %s" % exc)


@pytest.fixture
def committed_document():
    """Create a real committed document and clean it up in ``finally``.

    Deliberately does NOT use ``db_session``: the claim needs two
    independent connections that can block on the same row lock, and the
    savepoint fixture's outer transaction would keep the row invisible to
    the second connection.
    """
    _require_database()

    document_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    user_id = uuid.uuid4()

    setup = SessionLocal()
    try:
        setup.add_all(
            [
                User(
                    id=user_id,
                    email="claim-%s@example.com" % uuid.uuid4().hex,
                    password_hash="not-used-by-the-worker",
                ),
                Workspace(id=workspace_id, name="ws-%s" % uuid.uuid4().hex),
            ]
        )
        setup.flush()
        setup.add(
            Document(
                id=document_id,
                workspace_id=workspace_id,
                uploaded_by=user_id,
                original_filename="report.pdf",
                storage_key=storage_key_for(document_id),
                mime_type="application/pdf",
                size_bytes=len(PDF_BYTES),
                checksum=hashlib.sha256(PDF_BYTES).hexdigest(),
                status="queued",
                processing_attempts=0,
            )
        )
        setup.commit()
    finally:
        setup.close()

    try:
        yield {"document_id": document_id, "workspace_id": workspace_id}
    finally:
        cleanup = SessionLocal()
        try:
            cleanup.execute(
                delete(ProcessingRun).where(
                    ProcessingRun.document_id == document_id
                )
            )
            cleanup.execute(delete(Document).where(Document.id == document_id))
            cleanup.execute(
                delete(Workspace).where(Workspace.id == workspace_id)
            )
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()
        finally:
            cleanup.close()


def _claim_on_its_own_connection(
    document_id: uuid.UUID,
    barrier: threading.Barrier,
) -> ClaimedDocument | None:
    """Open a session, wait for the other thread, then claim."""
    session = SessionLocal()
    try:
        barrier.wait(timeout=10)
        return claim_document(session, document_id, max_attempts=5)
    finally:
        session.close()


def test_two_connections_claim_concurrently_with_one_winner(
    committed_document,
):
    barrier = threading.Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                _claim_on_its_own_connection,
                committed_document["document_id"],
                barrier,
            )
            for _ in range(2)
        ]
        results = [future.result(timeout=30) for future in futures]

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0].attempt_no == 1
    assert winners[0].workspace_id == committed_document["workspace_id"]

    check = SessionLocal()
    try:
        document = check.get(Document, committed_document["document_id"])
        runs = list(
            check.execute(
                select(ProcessingRun).where(
                    ProcessingRun.document_id
                    == committed_document["document_id"]
                )
            ).scalars()
        )
    finally:
        check.close()

    assert document is not None
    assert document.status == "processing"
    assert document.processing_attempts == 1
    assert document.retryable is False
    assert len(runs) == 1
    assert runs[0].attempt_no == 1
    assert runs[0].stage == "claimed"
