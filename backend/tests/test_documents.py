"""Tests for the document routes (Phase 2, step 2.5).

Covers upload validation, workspace isolation, role enforcement, status
transitions and the authorized download path, against a real PostgreSQL
database and the real local filesystem.
"""

import io
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.service import create_access_token
from app.db.models import Document, User, Workspace, WorkspaceMembership
from app.main import app
from app.storage import uploads

client = TestClient(app, raise_server_exceptions=False)

# A minimal but genuinely valid PDF header plus some content.
PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"


def _pdf(content: bytes = PDF_BYTES) -> bytes:
    return content


@pytest.fixture(autouse=True)
def clean_uploaded_files():
    """Remove files written by the test.

    The database transaction is rolled back, but the bytes on disk are
    not, so each test removes whatever it created.
    """
    root = uploads.storage_root() / "uploads"
    before = set(root.iterdir()) if root.exists() else set()
    yield
    after = set(root.iterdir()) if root.exists() else set()
    for entry in after - before:
        if entry.is_dir():
            for child in entry.rglob("*"):
                if child.is_file():
                    child.unlink()
            for child in sorted(entry.rglob("*"), reverse=True):
                if child.is_dir():
                    child.rmdir()
            entry.rmdir()
        else:
            entry.unlink()


def _make_user(db_session, workspace, role):
    """Create a user holding `role` in the given workspace."""
    user = User(
        email="u-%s@example.com" % uuid.uuid4().hex,
        password_hash="$2b$12$APB7JBc6bwnP9us0otNUAe4MlRYIcY0azrFhCdpCYuxWm.CxSjaI.",
    )
    db_session.add(user)
    db_session.flush()
    db_session.add(
        WorkspaceMembership(
            user_id=user.id, workspace_id=workspace.id, role=role
        )
    )
    db_session.flush()
    return {
        "user_id": user.id,
        "email": user.email,
        "workspace_id": workspace.id,
        "role": role,
    }


def _make_workspace(db_session):
    workspace = Workspace(name="ws-%s" % uuid.uuid4().hex)
    db_session.add(workspace)
    db_session.flush()
    return workspace


@pytest.fixture
def member(db_session):
    """A user with the 'member' role in one workspace."""
    workspace = _make_workspace(db_session)
    return _make_user(db_session, workspace, "member")


@pytest.fixture
def viewer(db_session):
    """A user with the 'viewer' role in one workspace."""
    workspace = _make_workspace(db_session)
    return _make_user(db_session, workspace, "viewer")


@pytest.fixture
def admin(db_session):
    """A user with the 'admin' role in one workspace."""
    workspace = _make_workspace(db_session)
    return _make_user(db_session, workspace, "admin")


@pytest.fixture
def shared_workspace(db_session):
    """A workspace plus a member and a viewer who are both in it."""
    workspace = _make_workspace(db_session)
    return {
        "workspace_id": workspace.id,
        "member": _make_user(db_session, workspace, "member"),
        "viewer": _make_user(db_session, workspace, "viewer"),
    }


def _auth(account, workspace_id=None):
    headers = {"Authorization": "Bearer %s" % create_access_token(account["user_id"])}
    if workspace_id is not None:
        headers["X-Workspace-ID"] = str(workspace_id)
    return headers


def _upload(account, content=PDF_BYTES, filename="report.pdf"):
    return client.post(
        "/api/v1/documents",
        files={"file": (filename, io.BytesIO(content), "application/pdf")},
        headers=_auth(account),
    )


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def test_upload_requires_authentication():
    r = client.post(
        "/api/v1/documents",
        files={"file": ("a.pdf", io.BytesIO(PDF_BYTES), "application/pdf")},
    )
    assert r.status_code == 401


def test_upload_by_a_viewer_is_forbidden(viewer):
    """viewers may read but not upload."""
    r = _upload(viewer)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


def test_upload_returns_202_and_queues_the_document(member):
    r = _upload(member)
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    assert body["document_id"]


def test_upload_persists_the_document_record(member, db_session):
    r = _upload(member)
    document_id = uuid.UUID(r.json()["document_id"])

    document = db_session.execute(
        select(Document).where(Document.id == document_id)
    ).scalar_one()
    assert document.workspace_id == member["workspace_id"]
    assert document.uploaded_by == member["user_id"]
    assert document.status == "queued"
    assert document.mime_type == "application/pdf"
    assert document.size_bytes == len(PDF_BYTES)
    assert document.checksum
    assert document.storage_key == "uploads/%s/original.pdf" % document_id


def test_upload_writes_the_file_to_the_agreed_layout(member, db_session):
    r = _upload(member)
    document_id = uuid.UUID(r.json()["document_id"])

    path = uploads.absolute_path("uploads/%s/original.pdf" % document_id)
    assert path.exists()
    assert path.read_bytes() == PDF_BYTES


def test_upload_rejects_a_non_pdf_extension(member):
    r = _upload(member, content=b"plain text", filename="notes.txt")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_upload_rejects_renamed_non_pdf_content(member):
    """A .pdf extension with the wrong content is not a PDF."""
    r = _upload(member, content=b"this is not a pdf at all", filename="fake.pdf")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_upload_rejects_an_empty_file(member):
    r = _upload(member, content=b"", filename="empty.pdf")
    assert r.status_code == 400


def test_upload_rejects_an_oversized_file(member, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "max_upload_bytes", 10)
    r = _upload(member, content=b"%PDF-1.7" + b"x" * 100)
    assert r.status_code == 400


def test_upload_strips_a_path_from_the_filename(member, db_session):
    r = _upload(member, filename="../../etc/passwd.pdf")
    assert r.status_code == 202
    document_id = uuid.UUID(r.json()["document_id"])
    document = db_session.execute(
        select(Document).where(Document.id == document_id)
    ).scalar_one()
    assert "/" not in document.original_filename
    assert ".." not in document.original_filename
    assert document.storage_key == "uploads/%s/original.pdf" % document_id


def test_upload_leaves_no_record_when_validation_fails(member, db_session):
    before = db_session.execute(select(Document)).scalars().all()
    count_before = len(before)
    _upload(member, content=b"nope", filename="bad.pdf")
    after = db_session.execute(select(Document)).scalars().all()
    assert len(after) == count_before


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

def test_list_requires_authentication():
    r = client.get("/api/v1/documents")
    assert r.status_code == 401


def test_list_returns_only_the_callers_workspace(member, db_session):
    _upload(member)

    other = Workspace(name="other-%s" % uuid.uuid4().hex)
    db_session.add(other)
    db_session.flush()
    db_session.add(
        Document(
            workspace_id=other.id,
            uploaded_by=other.id,
            original_filename="secret.pdf",
            storage_key="uploads/%s/original.pdf" % uuid.uuid4(),
            mime_type="application/pdf",
            size_bytes=10,
            checksum="x" * 64,
            status="indexed",
        )
    )
    db_session.flush()

    r = client.get("/api/v1/documents", headers=_auth(member))
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert all(item["original_filename"] != "secret.pdf" for item in body["items"])


def test_list_filters_by_status(member):
    _upload(member)
    r = client.get("/api/v1/documents?status_filter=queued", headers=_auth(member))
    assert r.status_code == 200
    assert r.json()["total"] == 1

    r2 = client.get("/api/v1/documents?status_filter=indexed", headers=_auth(member))
    assert r2.json()["total"] == 0


def test_list_filters_by_filename(member):
    _upload(member, filename="alpha.pdf")
    r = client.get("/api/v1/documents?q=alpha", headers=_auth(member))
    assert r.json()["total"] == 1

    r2 = client.get("/api/v1/documents?q=zzz", headers=_auth(member))
    assert r2.json()["total"] == 0


def test_list_pagination_is_stable(member):
    for i in range(3):
        _upload(member, filename="doc-%d.pdf" % i)
    r = client.get("/api/v1/documents?limit=2&offset=0", headers=_auth(member))
    assert r.json()["total"] == 3
    assert len(r.json()["items"]) == 2

    r2 = client.get("/api/v1/documents?limit=2&offset=2", headers=_auth(member))
    assert len(r2.json()["items"]) == 1


# ---------------------------------------------------------------------------
# Reading one document
# ---------------------------------------------------------------------------

def test_get_document_returns_metadata(member):
    r = _upload(member)
    document_id = r.json()["document_id"]

    r2 = client.get("/api/v1/documents/%s" % document_id, headers=_auth(member))
    assert r2.status_code == 200
    body = r2.json()
    assert body["id"] == document_id
    assert body["status"] == "queued"
    assert body["original_filename"] == "report.pdf"


def test_get_document_in_another_workspace_is_404(member, admin):
    """A document from another workspace must be invisible, not forbidden.

    A 403 would confirm that the id exists.
    """
    r = _upload(admin)
    document_id = r.json()["document_id"]

    r2 = client.get("/api/v1/documents/%s" % document_id, headers=_auth(member))
    assert r2.status_code == 404
    assert r2.json()["error"]["code"] == "NOT_FOUND"


def test_get_unknown_document_is_404(member):
    r = client.get("/api/v1/documents/%s" % uuid.uuid4(), headers=_auth(member))
    assert r.status_code == 404


def test_get_document_rejects_a_malformed_id(member):
    r = client.get("/api/v1/documents/not-a-uuid", headers=_auth(member))
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def test_download_returns_the_bytes(member):
    r = _upload(member)
    document_id = r.json()["document_id"]

    r2 = client.get("/api/v1/documents/%s/file" % document_id, headers=_auth(member))
    assert r2.status_code == 200
    assert r2.content == PDF_BYTES
    assert r2.headers["content-type"].startswith("application/pdf")


def test_download_from_another_workspace_is_404(member, admin):
    r = _upload(admin)
    document_id = r.json()["document_id"]

    r2 = client.get("/api/v1/documents/%s/file" % document_id, headers=_auth(member))
    assert r2.status_code == 404


def test_download_requires_authentication(member):
    r = _upload(member)
    document_id = r.json()["document_id"]
    r2 = client.get("/api/v1/documents/%s/file" % document_id)
    assert r2.status_code == 401


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------

def test_retry_requires_a_failed_document(member, db_session):
    r = _upload(member)
    document_id = uuid.UUID(r.json()["document_id"])

    r2 = client.post(
        "/api/v1/documents/%s/retry" % document_id, headers=_auth(member)
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "INVALID_STATE"


def test_retry_works_on_a_failed_document(member, db_session):
    r = _upload(member)
    document_id = uuid.UUID(r.json()["document_id"])

    document = db_session.execute(
        select(Document).where(Document.id == document_id)
    ).scalar_one()
    document.status = "failed"
    document.error_code = "CORRUPT_PDF"
    document.error_message = "The PDF did not contain readable text."
    db_session.flush()

    r2 = client.post(
        "/api/v1/documents/%s/retry" % document_id, headers=_auth(member)
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "queued"

    db_session.expire(document)
    assert document.status == "queued"
    assert document.error_code is None
    assert document.error_message is None
    assert document.processing_attempts == 1


def test_retry_in_another_workspace_is_404(member, admin):
    r = _upload(admin)
    document_id = r.json()["document_id"]
    r2 = client.post(
        "/api/v1/documents/%s/retry" % document_id, headers=_auth(member)
    )
    assert r2.status_code == 404


def test_retry_by_a_viewer_in_the_same_workspace_is_forbidden(shared_workspace):
    """A viewer cannot retry. The 403 is about the role, not about the id."""
    r = _upload(shared_workspace["member"])
    document_id = r.json()["document_id"]

    r2 = client.post(
        "/api/v1/documents/%s/retry" % document_id,
        headers=_auth(shared_workspace["viewer"]),
    )
    assert r2.status_code == 403
    assert r2.json()["error"]["code"] == "FORBIDDEN"


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

def test_delete_requires_admin(member):
    r = _upload(member)
    document_id = r.json()["document_id"]
    r2 = client.delete("/api/v1/documents/%s" % document_id, headers=_auth(member))
    assert r2.status_code == 403


def test_delete_by_a_member_in_the_same_workspace_is_forbidden(shared_workspace):
    """Members may upload but not delete."""
    r = _upload(shared_workspace["member"])
    document_id = r.json()["document_id"]

    r2 = client.delete(
        "/api/v1/documents/%s" % document_id,
        headers=_auth(shared_workspace["member"]),
    )
    assert r2.status_code == 403
    assert r2.json()["error"]["code"] == "FORBIDDEN"


def test_delete_by_admin_removes_the_row_and_the_file(admin, db_session):
    r = _upload(admin)
    document_id = uuid.UUID(r.json()["document_id"])
    path = uploads.absolute_path("uploads/%s/original.pdf" % document_id)
    assert path.exists()

    r2 = client.delete("/api/v1/documents/%s" % document_id, headers=_auth(admin))
    assert r2.status_code == 204

    remaining = db_session.execute(
        select(Document).where(Document.id == document_id)
    ).scalar_one_or_none()
    assert remaining is None
    assert not path.exists()


def test_delete_is_blocked_while_processing(admin, db_session):
    r = _upload(admin)
    document_id = uuid.UUID(r.json()["document_id"])

    document = db_session.execute(
        select(Document).where(Document.id == document_id)
    ).scalar_one()
    document.status = "processing"
    db_session.flush()

    r2 = client.delete("/api/v1/documents/%s" % document_id, headers=_auth(admin))
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "INVALID_STATE"


def test_delete_in_another_workspace_is_404(member, admin):
    r = _upload(admin)
    document_id = r.json()["document_id"]
    r2 = client.delete("/api/v1/documents/%s" % document_id, headers=_auth(member))
    assert r2.status_code == 404


def test_delete_unknown_document_is_404(admin):
    r = client.delete("/api/v1/documents/%s" % uuid.uuid4(), headers=_auth(admin))
    assert r.status_code == 404
