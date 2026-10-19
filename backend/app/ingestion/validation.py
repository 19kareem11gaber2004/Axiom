"""Validation of uploaded documents.

Phase 2 accepts PDF only. The checks are deliberately cheap and happen
before anything is written to disk or to the database, so a rejected
upload leaves no trace.
"""

import posixpath
import unicodedata
from typing import Optional

# Every PDF starts with this signature ("%PDF-").
PDF_MAGIC = b"%PDF-"

# Extensions we accept for a PDF upload.
ALLOWED_EXTENSIONS = (".pdf",)

# Characters that must never appear in a stored filename.
_UNSAFE_FILENAME_CHARS = '<>:"/\\|?*\x00'


class InvalidUploadError(Exception):
    """The uploaded file is not an acceptable PDF."""


def normalise_filename(filename: Optional[str]) -> str:
    """Make a user-supplied filename safe to store and to display.

    The filename is never used to build a path, but it is echoed back in
    API responses, so it is stripped of directory components and control
    characters.
    """
    if not filename:
        return "upload.pdf"

    # Drop any directory part: "C:\\Users\\x\\a.pdf" -> "a.pdf".
    name = filename.replace("\\", "/")
    name = posixpath.basename(name)
    # Normalise unicode so the name is stable across platforms.
    name = unicodedata.normalize("NFKC", name).strip()
    name = "".join(ch for ch in name if ch >= " " and ch not in _UNSAFE_FILENAME_CHARS)
    if not name:
        return "upload.pdf"
    return name[:255]


def has_allowed_extension(filename: str) -> bool:
    """True when the filename ends with an accepted extension."""
    lowered = filename.lower()
    return any(lowered.endswith(ext) for ext in ALLOWED_EXTENSIONS)


def looks_like_pdf(content: bytes) -> bool:
    """True when the content starts with the PDF signature."""
    return content.startswith(PDF_MAGIC)


def validate_pdf_upload(
    filename: str,
    content: bytes,
    max_bytes: int,
) -> None:
    """Validate an uploaded file, raising InvalidUploadError on failure.

    Checks, in order:
      1. the file is not empty;
      2. the extension is accepted;
      3. the content is within the size limit;
      4. the content really is a PDF (magic bytes), not a renamed file.
    """
    if not content:
        raise InvalidUploadError("The uploaded file is empty.")

    if not has_allowed_extension(filename):
        raise InvalidUploadError(
            "Only PDF files are accepted (expected a .pdf extension)."
        )

    if len(content) > max_bytes:
        raise InvalidUploadError(
            "The file is larger than the %d byte limit." % max_bytes
        )

    if not looks_like_pdf(content):
        raise InvalidUploadError(
            "The uploaded file is not a readable PDF."
        )
