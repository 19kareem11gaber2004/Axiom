"""Local-filesystem implementation of the storage port.

The on-disk layout is unchanged from Phase 2:

    <storage_root>/uploads/{document_id}/original.pdf

Two properties matter to the rest of the application:

* Writes are atomic. The bytes go to a temporary file in the destination
  directory, are fsynced, and are then moved into place with
  ``os.replace``. A reader sees either the previous object or the
  complete new one, never a partial upload.
* Keys cannot escape the storage root. Keys are POSIX-style relative
  paths; the adapter rejects absolute paths, ``..`` components, and
  paths that resolve outside the root (including through a symlink).
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from app.storage.base import (
    StorageNotFoundError,
    StoragePathError,
    StorageUnavailableError,
)
from app.storage.keys import validate_storage_key

__all__ = ["LocalFileStorage", "resolve_storage_root"]

# The worker runs as a different uid from the API in Compose, so a file
# created by mkstemp with its private 0600 mode would be unreadable to the
# worker. Match the Phase 2 helper's normal 0644 result while the file is
# still in the temporary location.
_STORED_FILE_MODE = 0o644

# A relative STORAGE_ROOT is resolved against the backend directory, not
# the process working directory. This preserves the Phase 2 behaviour and
# means the host and the container agree on what "storage" means:
#   backend/app/storage/local.py -> backend/
_BACKEND_DIR = Path(__file__).resolve().parents[2]


def resolve_storage_root(root: str | Path) -> Path:
    """Return the absolute, resolved storage root for ``root``.

    Relative paths are anchored at the backend directory so the result
    does not depend on where the process was started. The path is resolved
    before it is used for containment checks, so a symlinked root and a
    direct path compare consistently.
    """
    path = Path(root).expanduser()
    if not path.is_absolute():
        path = _BACKEND_DIR / path
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise StorageUnavailableError(
            "Could not resolve storage root %r." % str(root)
        ) from exc


class LocalFileStorage:
    """Store objects as files under a single local root directory."""

    def __init__(self, root: str | Path) -> None:
        self._root = resolve_storage_root(root)

    @property
    def root(self) -> Path:
        """The absolute, resolved root directory."""
        return self._root

    def __repr__(self) -> str:
        return "%s(root=%r)" % (type(self).__name__, str(self._root))

    def _path_for(self, key: str) -> Path:
        """Map a storage key to a path, rejecting anything outside root."""
        validate_storage_key(key)
        parts = key.split("/")
        try:
            target = self._root.joinpath(*parts).resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise StoragePathError("Invalid storage key %r." % key) from exc
        if target == self._root or self._root not in target.parents:
            raise StoragePathError(
                "Storage key %r escapes the storage root." % key
            )
        return target

    def save(self, key: str, content: bytes) -> None:
        """Write ``content`` at ``key`` atomically.

        The temporary file is created in the destination directory so
        ``os.replace`` never has to cross a filesystem boundary. The file
        is fsynced before the replace; a crash therefore leaves either the
        previous object or the complete new object, not a partial file.
        """
        target = self._path_for(key)
        temp_path: Path | None = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=target.parent,
                prefix=".axiom-upload-",
                suffix=".part",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, _STORED_FILE_MODE)
            os.replace(temp_path, target)
            temp_path = None
        except OSError as exc:
            raise StorageUnavailableError(
                "Could not save storage object %r." % key
            ) from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    # Never mask the original write error with a cleanup
                    # failure.
                    pass

    def read(self, key: str) -> bytes:
        """Return the complete object at ``key``."""
        target = self._path_for(key)
        try:
            return target.read_bytes()
        except FileNotFoundError as exc:
            raise StorageNotFoundError(
                "Storage object %r does not exist." % key
            ) from exc
        except OSError as exc:
            raise StorageUnavailableError(
                "Could not read storage object %r." % key
            ) from exc

    def delete(self, key: str) -> bool:
        """Delete the object at ``key``.

        Missing objects return ``False`` rather than raising, so retrying
        a delete is idempotent. Empty per-document directories are pruned
        up to, but never including, the storage root.
        """
        target = self._path_for(key)
        try:
            target.unlink()
        except FileNotFoundError:
            return False
        except IsADirectoryError as exc:
            raise StorageUnavailableError(
                "Storage object %r is not a regular file." % key
            ) from exc
        except OSError as exc:
            raise StorageUnavailableError(
                "Could not delete storage object %r." % key
            ) from exc

        parent = target.parent
        while parent != self._root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        return True

    def exists(self, key: str) -> bool:
        """Return whether a regular object exists at ``key``."""
        target = self._path_for(key)
        try:
            mode = os.stat(target).st_mode
        except (FileNotFoundError, NotADirectoryError):
            return False
        except OSError as exc:
            raise StorageUnavailableError(
                "Could not stat storage object %r." % key
            ) from exc
        return stat.S_ISREG(mode)

    def size(self, key: str) -> int:
        """Return the object's size in bytes."""
        target = self._path_for(key)
        try:
            info = os.stat(target)
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise StorageNotFoundError(
                "Storage object %r does not exist." % key
            ) from exc
        except OSError as exc:
            raise StorageUnavailableError(
                "Could not stat storage object %r." % key
            ) from exc
        if not stat.S_ISREG(info.st_mode):
            raise StorageUnavailableError(
                "Storage object %r is not a regular file." % key
            )
        return info.st_size
