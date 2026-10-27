"""End-to-end worker smoke test for the Compose CI stack.

Run this inside the API container after the API, worker, and Beat are up:

    python scripts/worker_smoke.py

The script seeds a unique user and workspace, logs in over HTTP, uploads a
minimal valid PDF, waits for the worker to mark it indexed, and verifies the
durable worker state in PostgreSQL. It always removes the rows and stored
object it created.

The API is addressed over loopback inside the container, so this works
without publishing the API port to the CI host.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, func, select  # noqa: E402

from app.auth.service import hash_password  # noqa: E402
from app.db.models import (  # noqa: E402
    Chunk,
    Document,
    ProcessingRun,
    User,
    Workspace,
    WorkspaceMembership,
)
from app.db.session import SessionLocal, close_session  # noqa: E402
from app.storage import get_storage  # noqa: E402

BASE_URL = os.environ.get(
    "AXIOM_SMOKE_BASE_URL",
    "http://127.0.0.1:8000",
).rstrip("/")
PASSWORD = "worker-smoke-password"
POLL_TIMEOUT_SECONDS = 90.0
POLL_INTERVAL_SECONDS = 0.5

PDF_BYTES = (
    b"%PDF-1.7\n"
    b"1 0 obj\n"
    b"<< /Type /Catalog >>\n"
    b"endobj\n"
    b"trailer\n"
    b"%%EOF\n"
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _http(
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, bytes]:
    """Perform one HTTP request and return its status and body."""
    request = Request(
        BASE_URL + path,
        data=body,
        headers=headers or {},
        method=method,
    )
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(
            "%s %s returned HTTP %d: %s"
            % (method, path, exc.code, detail)
        ) from exc


def _json(body: bytes) -> dict[str, Any]:
    return json.loads(body.decode("utf-8"))


def _auth_headers(token: str, workspace_id: uuid.UUID) -> dict[str, str]:
    return {
        "Authorization": "Bearer %s" % token,
        "X-Workspace-ID": str(workspace_id),
    }


def _seed_account() -> tuple[uuid.UUID, uuid.UUID, str]:
    """Create a committed user, workspace, and owner membership."""
    suffix = uuid.uuid4().hex
    email = "worker-smoke-%s@example.com" % suffix

    session = SessionLocal()
    try:
        user = User(
            email=email,
            password_hash=hash_password(PASSWORD),
        )
        workspace = Workspace(name="worker-smoke-%s" % suffix)
        session.add_all([user, workspace])
        session.flush()
        session.add(
            WorkspaceMembership(
                user_id=user.id,
                workspace_id=workspace.id,
                role="owner",
            )
        )
        session.commit()
        return user.id, workspace.id, email
    except Exception:
        session.rollback()
        raise
    finally:
        close_session(session)


def _login(email: str, password: str) -> str:
    status, body = _http(
        "POST",
        "/api/v1/auth/login",
        headers={"Content-Type": "application/json"},
        body=json.dumps({"email": email, "password": password}).encode("utf-8"),
    )
    _require(status == 200, "login returned HTTP %d" % status)
    payload = _json(body)
    token = payload.get("access_token")
    _require(bool(token), "login response did not contain an access token")
    return str(token)


def _upload(token: str, workspace_id: uuid.UUID) -> uuid.UUID:
    """Upload the minimal PDF and return the new document id."""
    boundary = "----axiom-smoke-" + uuid.uuid4().hex
    prefix = (
        "--%s\r\n"
        'Content-Disposition: form-data; name="file"; '
        'filename="worker-smoke.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ) % boundary
    body = prefix.encode("ascii") + PDF_BYTES
    body += ("\r\n--%s--\r\n" % boundary).encode("ascii")

    status, response_body = _http(
        "POST",
        "/api/v1/documents",
        headers={
            **_auth_headers(token, workspace_id),
            "Content-Type": "multipart/form-data; boundary=%s" % boundary,
        },
        body=body,
    )
    _require(
        status == 202,
        "upload returned HTTP %d: %s"
        % (status, response_body.decode("utf-8", "replace")),
    )
    payload = _json(response_body)
    _require(
        payload.get("duplicate") is False,
        "upload unexpectedly returned a duplicate",
    )
    return uuid.UUID(payload["document_id"])


def _wait_for_indexed(
    token: str,
    workspace_id: uuid.UUID,
    document_id: uuid.UUID,
) -> dict[str, Any]:
    """Poll the API until the worker reaches the indexed state."""
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    last_status: str | None = None

    while time.monotonic() < deadline:
        status, body = _http(
            "GET",
            "/api/v1/documents/%s" % document_id,
            headers=_auth_headers(token, workspace_id),
        )
        _require(
            status == 200,
            "document lookup returned HTTP %d: %s"
            % (status, body.decode("utf-8", "replace")),
        )
        payload = _json(body)
        current_status = payload.get("status")
        if current_status != last_status:
            print(
                "document %s status=%s" % (document_id, current_status),
                flush=True,
            )
            last_status = current_status

        if current_status == "indexed":
            return payload
        if current_status == "failed":
            raise AssertionError(
                "document %s failed: code=%s message=%s"
                % (
                    document_id,
                    payload.get("error_code"),
                    payload.get("error_message"),
                )
            )

        time.sleep(POLL_INTERVAL_SECONDS)

    raise TimeoutError(
        "document %s did not reach indexed within %.0f seconds"
        % (document_id, POLL_TIMEOUT_SECONDS)
    )


def _assert_worker_state(
    document_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> None:
    """Verify the durable state the worker must have committed."""
    session = SessionLocal()
    try:
        document = session.get(Document, document_id)
        _require(document is not None, "worker smoke document disappeared")
        _require(
            document.workspace_id == workspace_id,
            "document belongs to the wrong workspace",
        )
        _require(
            document.status == "indexed",
            "document status is %r, not indexed" % document.status,
        )
        _require(
            document.processing_attempts == 1,
            "processing_attempts is %d, expected 1"
            % document.processing_attempts,
        )
        _require(
            document.indexed_at is not None,
            "indexed_at was not set",
        )

        runs = list(
            session.execute(
                select(ProcessingRun)
                .where(ProcessingRun.document_id == document_id)
                .order_by(ProcessingRun.attempt_no)
            ).scalars()
        )
        _require(
            len(runs) == 1,
            "expected exactly one processing run, found %d" % len(runs),
        )
        run = runs[0]
        _require(run.attempt_no == 1, "processing run attempt is not 1")
        _require(
            run.stage == "indexed",
            "processing run stage is %r, not indexed" % run.stage,
        )
        _require(
            run.finished_at is not None,
            "processing run is not finished",
        )

        chunk_count = session.execute(
            select(func.count())
            .select_from(Chunk)
            .where(Chunk.document_id == document_id)
        ).scalar_one()
        _require(
            chunk_count == 0,
            "expected zero chunks, found %d" % chunk_count,
        )
    finally:
        close_session(session)


def _cleanup(user_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
    """Remove the smoke document, membership, workspace, user, and file."""
    session = SessionLocal()
    storage_keys: list[str] = []
    try:
        storage_keys = list(
            session.execute(
                select(Document.storage_key).where(
                    Document.workspace_id == workspace_id
                )
            ).scalars()
        )
        session.execute(
            delete(Document).where(Document.workspace_id == workspace_id)
        )
        session.execute(
            delete(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace_id
            )
        )
        session.execute(delete(Workspace).where(Workspace.id == workspace_id))
        session.execute(delete(User).where(User.id == user_id))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        close_session(session)

    storage = get_storage()
    for key in storage_keys:
        try:
            storage.delete(key)
        except Exception as exc:
            print(
                "warning: could not delete stored object %s: %s"
                % (key, exc),
                flush=True,
            )


def main() -> int:
    user_id: uuid.UUID | None = None
    workspace_id: uuid.UUID | None = None
    try:
        user_id, workspace_id, email = _seed_account()
        print(
            "seeded user=%s workspace=%s" % (user_id, workspace_id),
            flush=True,
        )

        token = _login(email, PASSWORD)
        document_id = _upload(token, workspace_id)
        print("uploaded document=%s" % document_id, flush=True)

        _wait_for_indexed(token, workspace_id, document_id)
        _assert_worker_state(document_id, workspace_id)
        print(
            "verified document=%s attempts=1 run=indexed chunks=0"
            % document_id,
            flush=True,
        )
        return 0
    finally:
        if user_id is not None and workspace_id is not None:
            _cleanup(user_id, workspace_id)
            print(
                "cleaned up user=%s workspace=%s"
                % (user_id, workspace_id),
                flush=True,
            )


if __name__ == "__main__":
    raise SystemExit(main())
