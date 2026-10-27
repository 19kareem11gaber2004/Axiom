"""Recovery without Redis, exercised synchronously.

This is the CI recovery check. A broker outage is simulated with a
publisher that returns False: the upload must still commit a queued row.
The sweeper then runs with a working fake publisher, and the recovered
row must be claimable by the worker. Neither fake touches Redis.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.service import create_access_token
from app.config import settings
from app.db.models import Document, User, Workspace, WorkspaceMembership
from app.main import app
from app.storage import get_storage
from app.worker import sweeper
from app.worker.pipeline import claim_document
from tests.fakes.publisher import RecordingTaskPublisher

PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def isolated_session(db_session):
    """Shadow the two tables the sweeper scans.

    The development database can contain real stale documents. A global
    sweep must never publish or re-timestamp them from a test.
    """
    connection = db_session.get_bind()
    connection.exec_driver_sql(
        "CREATE TEMP TABLE documents "
        "(LIKE public.documents INCLUDING ALL) "
        "ON COMMIT PRESERVE ROWS"
    )
    connection.exec_driver_sql(
        "CREATE TEMP TABLE processing_runs "
        "(LIKE public.processing_runs INCLUDING ALL) "
        "ON COMMIT PRESERVE ROWS"
    )
    return db_session


@pytest.fixture
def recovery_storage(memory_storage):
    """Keep the upload out of the repository's storage directory."""
    app.dependency_overrides[get_storage] = lambda: memory_storage
    try:
        yield memory_storage
    finally:
        app.dependency_overrides.pop(get_storage, None)


@pytest.fixture
def recovery_account(isolated_session):
    suffix = uuid.uuid4().hex
    user = User(
        email="recovery-%s@example.com" % suffix,
        password_hash="not-used-by-this-test",
    )
    workspace = Workspace(name="recovery-%s" % suffix)
    isolated_session.add_all([user, workspace])
    isolated_session.flush()
    isolated_session.add(
        WorkspaceMembership(
            user_id=user.id,
            workspace_id=workspace.id,
            role="owner",
        )
    )
    isolated_session.flush()
    return {
        "headers": {
            "Authorization": "Bearer %s" % create_access_token(user.id),
            "X-Workspace-ID": str(workspace.id),
        },
        "workspace_id": workspace.id,
    }


def test_a_failed_publish_is_recovered_and_the_row_is_claimable(
    isolated_session,
    recovery_account,
    recovery_storage,
    recording_publisher,
):
    """A failed broker publish must be recoverable from the database."""
    recording_publisher.result = False

    response = client.post(
        "/api/v1/documents",
        headers=recovery_account["headers"],
        files={"file": ("smoke.pdf", PDF_BYTES, "application/pdf")},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    document_id = uuid.UUID(body["document_id"])
    assert body["status"] == "queued"
    assert body["duplicate"] is False
    assert recording_publisher.calls == [(document_id, 0)]

    document = isolated_session.get(Document, document_id)
    assert document is not None
    assert document.status == "queued"
    assert document.processing_attempts == 0

    # The queued rule only recovers rows older than the configured cutoff.
    document.updated_at = datetime.now(timezone.utc) - timedelta(days=1)
    isolated_session.commit()

    working_publisher = RecordingTaskPublisher()
    summary = sweeper.sweep(isolated_session, working_publisher)

    assert summary.scanned == 1
    assert summary.recovered == 1
    assert summary.publish_failures == 0
    assert summary.more_remaining is False
    assert working_publisher.calls == [(document_id, 0)]

    claimed = claim_document(
        isolated_session,
        document_id,
        max_attempts=settings.max_processing_attempts,
    )
    assert claimed is not None
    assert claimed.id == document_id
    assert claimed.attempt_no == 1

    isolated_session.expire_all()
    stored = isolated_session.get(Document, document_id)
    assert stored is not None
    assert stored.status == "processing"
    assert stored.processing_attempts == 1


def test_both_phase4_tasks_are_registered():
    from app.worker import tasks
    from app.worker.celery_app import celery_app

    assert tasks.process_document.name == "documents.process"
    assert tasks.sweep_documents.name == "documents.sweep"
    assert celery_app.tasks["documents.process"].name == "documents.process"
    assert celery_app.tasks["documents.sweep"].name == "documents.sweep"

    entry = celery_app.conf.beat_schedule["sweep-stuck-documents"]
    assert entry["task"] == "documents.sweep"
    assert entry["schedule"] == settings.sweeper_interval_seconds
