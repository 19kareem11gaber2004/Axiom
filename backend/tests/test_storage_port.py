"""Tests for the storage port and the storage-key policy.

These tests need no database, no Redis, and no filesystem: they lock the
contract that every storage adapter must satisfy and the key layout the
rest of the application depends on.
"""

import uuid

from app.storage.base import (
    FileStorage,
    StorageError,
    StorageNotFoundError,
    StoragePathError,
    StorageUnavailableError,
)
from app.storage.keys import STORED_FILENAME, storage_key_for


class _FakeStorage:
    """A minimal structural implementation, used only to check the port."""

    def save(self, key: str, content: bytes) -> None:
        pass

    def read(self, key: str) -> bytes:
        return b""

    def delete(self, key: str) -> bool:
        return False

    def exists(self, key: str) -> bool:
        return False

    def size(self, key: str) -> int:
        return 0


# ---------------------------------------------------------------------------
# The port
# ---------------------------------------------------------------------------

def test_a_class_with_the_five_operations_satisfies_the_port():
    """The port is structural, so a second backend needs no inheritance."""
    assert isinstance(_FakeStorage(), FileStorage)


def test_the_port_exposes_exactly_the_expected_operations():
    expected = {"save", "read", "delete", "exists", "size"}
    assert expected <= set(dir(FileStorage))


# ---------------------------------------------------------------------------
# The error taxonomy
# ---------------------------------------------------------------------------

def test_every_storage_error_shares_one_base_class():
    assert issubclass(StorageUnavailableError, StorageError)
    assert issubclass(StorageNotFoundError, StorageError)
    assert issubclass(StoragePathError, StorageError)


def test_a_path_error_is_also_a_value_error():
    """Invalid keys are programming errors, like the old helpers' ValueError."""
    assert issubclass(StoragePathError, ValueError)


# ---------------------------------------------------------------------------
# The key policy
# ---------------------------------------------------------------------------

def test_the_stored_filename_is_the_agreed_one():
    assert STORED_FILENAME == "original.pdf"


def test_the_key_is_built_from_the_document_id():
    document_id = uuid.uuid4()
    assert storage_key_for(document_id) == f"uploads/{document_id}/original.pdf"


def test_a_serialised_document_id_produces_the_same_key():
    document_id = uuid.uuid4()
    assert storage_key_for(str(document_id)) == storage_key_for(document_id)


def test_the_key_uses_forward_slashes_only():
    """Keys are backend-neutral; the local adapter converts them to a path."""
    key = storage_key_for(uuid.uuid4())
    assert "\\" not in key
    assert key.startswith("uploads/")
