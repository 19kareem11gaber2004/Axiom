"""In-memory implementation of the storage port, for tests only.

The fake deliberately reuses :func:`app.storage.keys.validate_storage_key`
instead of maintaining a second path-validation policy. That keeps the
contract tests honest: both adapters must reject the same malformed keys,
including Windows-style paths when the suite runs on Linux.
"""

from __future__ import annotations

from app.storage.base import StorageNotFoundError
from app.storage.keys import validate_storage_key


class InMemoryFileStorage:
    """Store object bytes in a dictionary; never touch the filesystem."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def __repr__(self) -> str:
        return "%s(objects=%d)" % (type(self).__name__, len(self._objects))

    def save(self, key: str, content: bytes) -> None:
        """Store ``content`` at ``key``, replacing any existing object."""
        validate_storage_key(key)
        self._objects[key] = bytes(content)

    def read(self, key: str) -> bytes:
        """Return the bytes stored at ``key``."""
        validate_storage_key(key)
        try:
            return self._objects[key]
        except KeyError as exc:
            raise StorageNotFoundError(
                "Storage object %r does not exist." % key
            ) from exc

    def delete(self, key: str) -> bool:
        """Delete ``key``, returning False when it was already absent."""
        validate_storage_key(key)
        if key not in self._objects:
            return False
        del self._objects[key]
        return True

    def exists(self, key: str) -> bool:
        """Return whether an object is stored at ``key``."""
        validate_storage_key(key)
        return key in self._objects

    def size(self, key: str) -> int:
        """Return the stored object's size in bytes."""
        validate_storage_key(key)
        try:
            return len(self._objects[key])
        except KeyError as exc:
            raise StorageNotFoundError(
                "Storage object %r does not exist." % key
            ) from exc
