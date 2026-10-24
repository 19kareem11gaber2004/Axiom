"""Shared contract tests for every FileStorage implementation.

The same tests run against the real local adapter and the in-memory fake
via the ``storage_adapter`` fixture. Running one contract against both
implementations is what prevents them from silently drifting apart.
"""

from __future__ import annotations

import uuid

import pytest

from app.storage.base import (
    FileStorage,
    StorageNotFoundError,
    StoragePathError,
    StorageUnavailableError,
)
from app.storage.keys import storage_key_for


def _key() -> str:
    """A valid key in the layout the application actually uses."""
    return storage_key_for(uuid.uuid4())


def _assert_invalid_key(storage, key: str) -> None:
    """Every operation must reject an invalid key before touching state."""
    with pytest.raises(StoragePathError):
        storage.save(key, b"blocked")
    with pytest.raises(StoragePathError):
        storage.read(key)
    with pytest.raises(StoragePathError):
        storage.delete(key)
    with pytest.raises(StoragePathError):
        storage.exists(key)
    with pytest.raises(StoragePathError):
        storage.size(key)


# ---------------------------------------------------------------------------
# The shared contract
# ---------------------------------------------------------------------------

def test_both_adapters_satisfy_the_port(storage_adapter):
    assert isinstance(storage_adapter, FileStorage)


def test_save_and_read_preserve_bytes(storage_adapter):
    key = _key()
    content = b"%PDF-1.7\n\x00\xffbinary payload\n%%EOF\n"

    assert storage_adapter.exists(key) is False
    storage_adapter.save(key, content)

    assert storage_adapter.exists(key) is True
    assert storage_adapter.read(key) == content
    assert storage_adapter.size(key) == len(content)


def test_overwrite_replaces_content_and_size(storage_adapter):
    key = _key()

    storage_adapter.save(key, b"first")
    storage_adapter.save(key, b"second, longer")

    assert storage_adapter.read(key) == b"second, longer"
    assert storage_adapter.size(key) == len(b"second, longer")


def test_empty_content_is_a_real_object(storage_adapter):
    key = _key()

    storage_adapter.save(key, b"")

    assert storage_adapter.exists(key) is True
    assert storage_adapter.read(key) == b""
    assert storage_adapter.size(key) == 0


def test_missing_object_semantics(storage_adapter):
    key = _key()

    assert storage_adapter.exists(key) is False
    assert storage_adapter.delete(key) is False
    with pytest.raises(StorageNotFoundError):
        storage_adapter.read(key)
    with pytest.raises(StorageNotFoundError):
        storage_adapter.size(key)


def test_delete_removes_object_and_is_idempotent(storage_adapter):
    key = _key()
    storage_adapter.save(key, b"delete me")

    assert storage_adapter.delete(key) is True
    assert storage_adapter.exists(key) is False
    assert storage_adapter.delete(key) is False


# ---------------------------------------------------------------------------
# Key validation: the same policy on every backend and every platform
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "key",
    [
        "../secret.pdf",
        "uploads/../../secret.pdf",
        "uploads/../secret.pdf",
        "..",
        "uploads/..",
    ],
)
def test_traversal_keys_are_rejected(storage_adapter, key):
    _assert_invalid_key(storage_adapter, key)


@pytest.mark.parametrize(
    "key",
    [
        "/secret.pdf",
        "/etc/passwd",
        "//server/share/secret.pdf",
    ],
)
def test_absolute_paths_are_rejected(storage_adapter, key):
    _assert_invalid_key(storage_adapter, key)


@pytest.mark.parametrize(
    "key",
    [
        "C:\\secret.pdf",
        "C:/secret.pdf",
        "D:\\data\\file.pdf",
        "\\\\server\\share\\secret.pdf",
    ],
)
def test_windows_paths_are_rejected_on_every_platform(storage_adapter, key):
    _assert_invalid_key(storage_adapter, key)


@pytest.mark.parametrize(
    "key",
    [
        "",
        "uploads//secret.pdf",
        "uploads/./secret.pdf",
        "uploads/secret.pdf/",
        "uploads\\secret.pdf",
        "uploads/\x00secret.pdf",
    ],
)
def test_malformed_keys_are_rejected(storage_adapter, key):
    _assert_invalid_key(storage_adapter, key)


# ---------------------------------------------------------------------------
# Adapter-specific invariants
# ---------------------------------------------------------------------------

def test_local_storage_fixture_uses_an_isolated_root(local_storage, tmp_path):
    """Storage tests must never write into the repository's backend/storage."""
    assert local_storage.root == (tmp_path / "storage").resolve()
    assert tmp_path in local_storage.root.parents


@pytest.mark.parametrize("failure_point", ["fsync", "replace"])
@pytest.mark.parametrize("existing_object", [False, True], ids=["new", "overwrite"])
def test_local_atomic_write_failure_is_safe(
    local_storage, monkeypatch, failure_point, existing_object
):
    """A failed atomic write leaves neither a partial object nor a temp file."""
    key = _key()
    target = local_storage.root.joinpath(*key.split("/"))
    original = b"original bytes"

    if existing_object:
        local_storage.save(key, original)

    def fail(*args, **kwargs):
        raise OSError("simulated %s failure" % failure_point)

    if failure_point == "fsync":
        monkeypatch.setattr("app.storage.local.os.fsync", fail)
    else:
        monkeypatch.setattr("app.storage.local.os.replace", fail)

    with pytest.raises(StorageUnavailableError):
        local_storage.save(key, b"replacement bytes")

    if existing_object:
        assert local_storage.exists(key) is True
        assert local_storage.read(key) == original
        assert local_storage.size(key) == len(original)
        assert target.read_bytes() == original
    else:
        assert local_storage.exists(key) is False
        assert target.exists() is False

    assert not list(local_storage.root.rglob("*.part"))


# ---------------------------------------------------------------------------
# The FastAPI override helper used by route tests
# ---------------------------------------------------------------------------

def test_storage_dependency_override_returns_the_adapter(override_storage):
    from app.main import app
    from app.storage import get_storage

    assert app.dependency_overrides[get_storage]() is override_storage
