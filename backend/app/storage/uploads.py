"""Local upload storage.

Phase 2 stores uploaded files on the local filesystem using the layout
fixed in the proposal (docs/phase-1, section 11.1):

    <storage_root>/uploads/{document_id}/original.pdf

The FileStorage abstraction is deliberately NOT introduced yet: that is
Phase 3 work. The functions here are plain helpers so the interface can
be extracted around them later without changing the on-disk layout.
"""

import hashlib
import shutil
from pathlib import Path
from typing import Tuple

from app.config import settings

# The extension and name used for the stored object. The user's original
# filename is never used to build a path.
STORED_FILENAME = "original.pdf"


def storage_root() -> Path:
    """Absolute path of the storage root."""
    root = Path(settings.storage_root)
    if not root.is_absolute():
        root = Path(__file__).resolve().parent.parent.parent / root
    return root


def storage_key_for(document_id) -> str:
    """The relative key stored in the documents table."""
    return "uploads/%s/%s" % (document_id, STORED_FILENAME)


def absolute_path(storage_key: str) -> Path:
    """Resolve a storage key to an absolute path.

    Guards against a key that tries to escape the storage root.
    """
    root = storage_root().resolve()
    target = (root / storage_key).resolve()
    if root != target and root not in target.parents:
        raise ValueError("Storage key escapes the storage root.")
    return target


def save_upload(document_id, content: bytes) -> str:
    """Write the uploaded bytes and return the storage key.

    The directory is created if needed. Returns the key on success.
    """
    key = storage_key_for(document_id)
    target = absolute_path(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temporary file first, then move it into place, so a
    # crash can never leave a half-written file that a worker would
    # happily try to parse.
    temp = target.with_suffix(".partial")
    with open(temp, "wb") as handle:
        handle.write(content)
    temp.replace(target)
    return key


def read_upload(storage_key: str) -> bytes:
    """Read a stored object."""
    return absolute_path(storage_key).read_bytes()


def delete_upload(storage_key: str) -> bool:
    """Delete a stored object. Returns True if something was removed.

    Missing files are not an error: a delete must succeed even when the
    file was already gone.
    """
    try:
        target = absolute_path(storage_key)
    except ValueError:
        return False
    if not target.exists():
        return False
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
        return True
    target.unlink(missing_ok=True)
    # Remove the now-empty per-document directory.
    try:
        target.parent.rmdir()
    except OSError:
        pass
    return True


def checksum_of(content: bytes) -> str:
    """SHA-256 of the uploaded bytes."""
    return hashlib.sha256(content).hexdigest()


def file_size(storage_key: str) -> int:
    """Size in bytes of a stored object, or 0 if it is missing."""
    try:
        return absolute_path(storage_key).stat().st_size
    except (OSError, ValueError):
        return 0


def split_storage_key(storage_key: str) -> Tuple[str, str]:
    """Split a key into (document_id, filename). Used by tests and logs."""
    parts = storage_key.split("/")
    if len(parts) != 3:
        return ("", "")
    return (parts[1], parts[2])
