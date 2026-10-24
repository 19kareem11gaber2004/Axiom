"""Storage: the FileStorage port and the configured adapter.

Callers depend on :class:`~app.storage.base.FileStorage`, not on the
concrete implementation. ``get_storage()`` is the only place that turns
the ``STORAGE_BACKEND`` setting into an adapter, so swapping the local
filesystem for an object store later does not touch the routes or the
worker.
"""

from __future__ import annotations

from app.config import settings
from app.storage.base import (
    FileStorage,
    StorageError,
    StorageNotFoundError,
    StoragePathError,
    StorageUnavailableError,
)
from app.storage.local import LocalFileStorage

__all__ = [
    "FileStorage",
    "LocalFileStorage",
    "StorageError",
    "StorageNotFoundError",
    "StoragePathError",
    "StorageUnavailableError",
    "get_storage",
]


def get_storage() -> FileStorage:
    """Build the storage adapter selected by ``STORAGE_BACKEND``.

    The local adapter is stateless, so constructing a fresh instance per
    call is cheap and always reflects the current settings. Configuration
    validation rejects unknown backends at startup; the branch here is a
    second line of defence for callers that construct settings-like
    objects in tests.
    """
    if settings.storage_backend == "local":
        return LocalFileStorage(settings.storage_root)
    raise RuntimeError(
        "Unsupported storage backend %r. Settings validation should have "
        "rejected it at startup." % settings.storage_backend
    )
