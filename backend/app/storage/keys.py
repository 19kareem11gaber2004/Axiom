"""Storage-key policy.

A key is the stable name of an object inside the configured storage
root. It is deliberately independent of the backend: the same key works
for local disk today and for an object store later.

The user's original filename never participates in a key. Only the
server-generated document id does, so a filename such as
``../../etc/passwd.pdf`` cannot influence the path. The layout is fixed
by the Phase 1 design:

    <storage_root>/uploads/{document_id}/original.pdf
"""

from __future__ import annotations

import uuid
from pathlib import PureWindowsPath

from app.storage.base import StoragePathError

__all__ = ["STORED_FILENAME", "storage_key_for", "validate_storage_key"]

# Every document is stored under a fixed name. The user's filename is
# metadata, not a path component.
STORED_FILENAME = "original.pdf"


def storage_key_for(document_id: uuid.UUID | str) -> str:
    """Return the POSIX-style key for a document's original file.

    ``document_id`` is the UUID assigned by the server. A string is
    accepted so callers can pass a value that is already serialised; it
    is not validated here because every caller obtains it from the
    database or from ``uuid.uuid4()``.
    """
    return f"uploads/{document_id}/{STORED_FILENAME}"


def validate_storage_key(key: str) -> None:
    """Reject a key that is not a safe, backend-neutral relative path.

    The rule is deliberately independent of the local filesystem so the
    same key is accepted or rejected by every adapter. In particular, a
    Windows drive letter or backslash must be rejected even when the test
    suite is running on Linux: accepting it on one platform and rejecting
    it on another would make storage behavior depend on the host.

    This checks the key syntax only. The local adapter additionally
    resolves the key against its root and verifies that the result stays
    inside that root (a symlink can only be checked with the root known).
    """
    if not isinstance(key, str) or not key:
        raise StoragePathError("A storage key must be a non-empty string.")
    if "\x00" in key:
        raise StoragePathError("A storage key must not contain NUL bytes.")
    # Keys are POSIX-style. A backslash is a path separator on Windows, so
    # accepting it would make the same key mean different things on the
    # host and in the Linux container.
    if "\\" in key:
        raise StoragePathError(
            "A storage key must use forward slashes, not backslashes."
        )
    windows_path = PureWindowsPath(key)
    if key.startswith("/") or windows_path.is_absolute() or windows_path.drive:
        raise StoragePathError("A storage key must be a relative path.")
    parts = key.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise StoragePathError(
            "A storage key must not contain empty, '.' or '..' components."
        )
