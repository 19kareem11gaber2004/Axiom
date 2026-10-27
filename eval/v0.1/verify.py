"""Verify the frozen ``eval/v0.1`` corpus.

The default (offline) mode is standard-library only. It checks:

* the SHA-256 manifest against ``corpus.lock``, ``schema.json`` and
  ``seed-questions.jsonl``;
* the lock's document set, byte sizes, page counts and hashes;
* every question row against the frozen schema contract and the corpus
  distribution.

``--download`` additionally downloads each PDF from its recorded source,
re-checks the exact bytes and SHA-256, counts pages with pypdf, and proves
that every gold span occurs on the page it names. That mode needs network
access and ``pypdf``; it is the check the Phase 5 evaluation harness will
run before trusting an evaluation result.

Exit codes: 0 = verified, 1 = a corpus check failed or the optional live
download could not be completed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "MANIFEST.sha256"
LOCK = HERE / "corpus.lock"
SCHEMA = HERE / "schema.json"
ROWS = HERE / "seed-questions.jsonl"

EXPECTED_FILES = ("corpus.lock", "schema.json", "seed-questions.jsonl")
EXPECTED_DOCS = {
    "attention.pdf": 15,
    "bert.pdf": 16,
    "udhr_en.pdf": 8,
    "nist-ai-rmf.pdf": 48,
}
EXPECTED_CATEGORIES = {
    "fact": 7,
    "multi-chunk": 4,
    "cross-doc": 3,
    "unanswerable": 3,
    "isolation": 2,
    "citation": 1,
}
EXPECTED_OUTCOMES = {"answer": 15, "abstain": 5}
ROLES = {"owner", "admin", "member", "viewer"}
REQUIRED_ROW_KEYS = {
    "id",
    "question",
    "category",
    "answerable",
    "expected",
    "visible_docs",
    "requester",
    "gold",
    "key_facts",
}
OPTIONAL_ROW_KEYS = {"forbidden_docs", "missing_evidence"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^q[0-9]{2}$")
WHITESPACE_RE = re.compile(r"\s+")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


class CorpusError(Exception):
    """A frozen-corpus invariant failed."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CorpusError(message)


def normalized_text(value: str) -> str:
    """Collapse every whitespace run to one space for span matching."""

    return WHITESPACE_RE.sub(" ", value).strip()


def file_digest(path: Path) -> str:
    """Hash a UTF-8 text file with CRLF normalized to LF."""

    data = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(data).hexdigest()


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CorpusError(f"missing file: {path.name}") from exc
    except json.JSONDecodeError as exc:
        raise CorpusError(f"{path.name} is not valid JSON: {exc}") from exc


def load_rows() -> list[dict[str, Any]]:
    try:
        lines = ROWS.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise CorpusError(f"missing file: {ROWS.name}") from exc

    rows: list[dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CorpusError(
                f"seed-questions.jsonl line {number} is not valid JSON: {exc}"
            ) from exc
        require(isinstance(row, dict), f"line {number} is not a JSON object")
        rows.append(row)
    return rows


def verify_manifest() -> dict[str, str]:
    """Check that the three corpus artifacts match the frozen manifest."""

    try:
        lines = MANIFEST.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise CorpusError("missing file: MANIFEST.sha256") from exc

    entries: dict[str, str] = {}
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        require(
            len(parts) == 2,
            f"MANIFEST.sha256 line {number} must be '<sha256>  <file>'",
        )
        digest, name = parts
        require(
            SHA256_RE.fullmatch(digest) is not None,
            f"MANIFEST.sha256 line {number} has an invalid SHA-256",
        )
        require(
            name in EXPECTED_FILES,
            f"MANIFEST.sha256 names an unknown file: {name}",
        )
        require(name not in entries, f"MANIFEST.sha256 repeats {name}")
        entries[name] = digest

    require(
        set(entries) == set(EXPECTED_FILES),
        "MANIFEST.sha256 must pin exactly: " + ", ".join(EXPECTED_FILES),
    )
    for name, expected in entries.items():
        actual = file_digest(HERE / name)
        require(
            actual == expected,
            f"{name} has changed: manifest={expected} actual={actual}",
        )
    return entries


def verify_schema() -> dict[str, Any]:
    """Check the frozen schema still describes the row contract we validate."""

    schema = load_json(SCHEMA)
    require(isinstance(schema, dict), "schema.json must be a JSON object")
    require(schema.get("type") == "object", "schema.json must type the row as object")
    require(
        schema.get("additionalProperties") is False,
        "schema.json must reject additional properties",
    )
    properties = schema.get("properties")
    require(isinstance(properties, dict), "schema.json has no properties object")
    required = schema.get("required")
    require(isinstance(required, list), "schema.json has no required list")
    require(
        set(required) == REQUIRED_ROW_KEYS,
        "schema.json required keys do not match the verifier contract",
    )
    category_values = properties.get("category", {}).get("enum")
    require(
        isinstance(category_values, list)
        and set(category_values) == set(EXPECTED_CATEGORIES),
        "schema.json category enum does not match the frozen categories",
    )
    expected_values = properties.get("expected", {}).get("enum")
    require(
        isinstance(expected_values, list)
        and set(expected_values) == set(EXPECTED_OUTCOMES),
        "schema.json expected enum does not match the frozen outcomes",
    )
    return schema


def verify_lock() -> dict[str, Any]:
    """Check the frozen source documents and their integrity metadata."""

    lock = load_json(LOCK)
    require(isinstance(lock, dict), "corpus.lock must be a JSON object")
    require(lock.get("version") == "v0.1", "corpus.lock version must be v0.1")
    require(
        lock.get("frozen_at") == "2026-10-17",
        "corpus.lock frozen_at must remain 2026-10-17",
    )
    docs = lock.get("docs")
    require(isinstance(docs, list), "corpus.lock docs must be a list")
    require(len(docs) == len(EXPECTED_DOCS), "corpus.lock must contain four documents")

    seen: set[str] = set()
    pages = 0
    for index, doc in enumerate(docs):
        where = f"corpus.lock docs[{index}]"
        require(isinstance(doc, dict), f"{where} must be an object")
        for key in (
            "file",
            "source_url",
            "description",
            "bytes",
            "sha256",
            "pages",
            "license",
            "license_evidence",
        ):
            require(key in doc, f"{where} is missing {key}")
        name = doc["file"]
        require(isinstance(name, str) and name, f"{where}.file must be non-empty")
        require(name in EXPECTED_DOCS, f"{where} names an unexpected document: {name}")
        require(name not in seen, f"corpus.lock repeats {name}")
        seen.add(name)
        require(
            isinstance(doc["source_url"], str)
            and doc["source_url"].startswith("https://"),
            f"{where}.source_url must be an https URL",
        )
        require(
            isinstance(doc["bytes"], int) and doc["bytes"] > 0,
            f"{where}.bytes must be a positive integer",
        )
        require(
            isinstance(doc["pages"], int) and doc["pages"] == EXPECTED_DOCS[name],
            f"{where}.pages must remain {EXPECTED_DOCS[name]}",
        )
        require(
            isinstance(doc["sha256"], str)
            and SHA256_RE.fullmatch(doc["sha256"]) is not None,
            f"{where}.sha256 must be 64 lowercase hex characters",
        )
        require(
            isinstance(doc["description"], str) and doc["description"].strip(),
            f"{where}.description must be non-empty",
        )
        require(
            isinstance(doc["license"], str) and doc["license"].strip(),
            f"{where}.license must be non-empty",
        )
        require(
            isinstance(doc["license_evidence"], str)
            and doc["license_evidence"].strip(),
            f"{where}.license_evidence must be non-empty",
        )
        pages += doc["pages"]

    require(
        seen == set(EXPECTED_DOCS),
        "corpus.lock document set has changed",
    )
    require(pages == sum(EXPECTED_DOCS.values()), "corpus.lock page total has changed")
    return lock


def verify_rows(rows: list[dict[str, Any]], lock: dict[str, Any]) -> dict[str, Any]:
    """Check every question row against the frozen contract and distribution."""

    doc_pages = {doc["file"]: doc["pages"] for doc in lock["docs"]}
    require(len(rows) == 20, f"expected 20 questions, found {len(rows)}")
    expected_ids = [f"q{number:02d}" for number in range(1, 21)]
    actual_ids = [row.get("id") for row in rows]
    require(actual_ids == expected_ids, "question ids must be q01..q20 in order")

    categories: Counter[str] = Counter()
    outcomes: Counter[str] = Counter()
    workspaces: Counter[str] = Counter()

    for row in rows:
        where = row["id"]
        require(
            set(row) <= REQUIRED_ROW_KEYS | OPTIONAL_ROW_KEYS,
            f"{where}: unknown row keys {sorted(set(row) - REQUIRED_ROW_KEYS - OPTIONAL_ROW_KEYS)}",
        )
        require(
            REQUIRED_ROW_KEYS <= set(row),
            f"{where}: missing row keys {sorted(REQUIRED_ROW_KEYS - set(row))}",
        )
        require(
            isinstance(row["question"], str) and row["question"].strip(),
            f"{where}: question must be non-empty",
        )
        category = row["category"]
        require(category in EXPECTED_CATEGORIES, f"{where}: unknown category {category!r}")
        expected = row["expected"]
        require(expected in EXPECTED_OUTCOMES, f"{where}: unknown expected {expected!r}")
        require(isinstance(row["answerable"], bool), f"{where}: answerable must be boolean")
        require(
            row["answerable"] is (expected == "answer"),
            f"{where}: answerable must match expected",
        )

        visible = row["visible_docs"]
        require(isinstance(visible, list) and visible, f"{where}: visible_docs is empty")
        require(
            len(set(visible)) == len(visible),
            f"{where}: visible_docs contains duplicates",
        )
        for name in visible:
            require(isinstance(name, str) and name in doc_pages, f"{where}: unknown visible doc {name!r}")

        forbidden = row.get("forbidden_docs", [])
        require(isinstance(forbidden, list), f"{where}: forbidden_docs must be a list")
        require(
            len(set(forbidden)) == len(forbidden),
            f"{where}: forbidden_docs contains duplicates",
        )
        for name in forbidden:
            require(isinstance(name, str) and name in doc_pages, f"{where}: unknown forbidden doc {name!r}")
        require(
            not set(visible) & set(forbidden),
            f"{where}: a document cannot be both visible and forbidden",
        )

        requester = row["requester"]
        require(
            isinstance(requester, dict) and set(requester) == {"workspace", "role"},
            f"{where}: requester must contain exactly workspace and role",
        )
        require(
            isinstance(requester["workspace"], str) and requester["workspace"].strip(),
            f"{where}: requester.workspace must be non-empty",
        )
        require(requester["role"] in ROLES, f"{where}: unknown role {requester['role']!r}")

        gold = row["gold"]
        require(isinstance(gold, list), f"{where}: gold must be a list")
        if expected == "answer":
            require(gold, f"{where}: an answerable row needs at least one gold span")
            require(
                "missing_evidence" not in row,
                f"{where}: an answerable row must not define missing_evidence",
            )
            require(
                isinstance(row["key_facts"], list) and row["key_facts"],
                f"{where}: an answerable row needs key facts",
            )
        else:
            require(not gold, f"{where}: an abstention row must not have gold spans")
            require(
                isinstance(row.get("missing_evidence"), str)
                and row["missing_evidence"].strip(),
                f"{where}: an abstention row needs missing_evidence",
            )
            require(
                row["key_facts"] == [],
                f"{where}: an abstention row must not have key facts",
            )

        for index, span in enumerate(gold):
            span_where = f"{where} gold[{index}]"
            require(
                isinstance(span, dict) and set(span) == {"doc", "page", "span"},
                f"{span_where}: must contain exactly doc, page, span",
            )
            name = span["doc"]
            require(name in visible, f"{span_where}: {name!r} is not visible")
            require(
                isinstance(span["page"], int) and 1 <= span["page"] <= doc_pages[name],
                f"{span_where}: page is outside {name}",
            )
            require(
                isinstance(span["span"], str) and span["span"].strip(),
                f"{span_where}: span must be non-empty",
            )

        if category == "isolation":
            require(forbidden, f"{where}: isolation rows need forbidden_docs")
            require(expected == "abstain", f"{where}: isolation rows must abstain")
        else:
            require(
                "forbidden_docs" not in row,
                f"{where}: only isolation rows may define forbidden_docs",
            )

        categories[category] += 1
        outcomes[expected] += 1
        workspaces[requester["workspace"]] += 1

    require(
        dict(sorted(categories.items())) == dict(sorted(EXPECTED_CATEGORIES.items())),
        f"category distribution changed: {dict(categories)}",
    )
    require(
        dict(sorted(outcomes.items())) == dict(sorted(EXPECTED_OUTCOMES.items())),
        f"outcome distribution changed: {dict(outcomes)}",
    )
    require(
        workspaces == Counter({"ws-a": 18, "ws-b": 2}),
        f"requester workspace distribution changed: {dict(workspaces)}",
    )
    return {
        "questions": len(rows),
        "categories": dict(categories),
        "outcomes": dict(outcomes),
        "workspaces": dict(workspaces),
    }


def verify_offline() -> dict[str, Any]:
    """Run every check that does not need the network."""

    manifest = verify_manifest()
    schema = verify_schema()
    lock = verify_lock()
    rows = load_rows()
    summary = verify_rows(rows, lock)
    summary.update(
        {
            "documents": len(lock["docs"]),
            "pages": sum(doc["pages"] for doc in lock["docs"]),
            "manifest": manifest,
            "schema_title": schema.get("title"),
        }
    )
    return summary


def verify_downloads() -> dict[str, Any]:
    """Download every source and prove bytes, hashes, pages and gold spans."""

    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise CorpusError(
            "--download requires pypdf; install the Phase 5 evaluation dependencies"
        ) from exc

    try:
        lock = verify_lock()
        rows = load_rows()
        verify_rows(rows, lock)
    except CorpusError:
        raise

    page_text: dict[str, list[str]] = {}
    verified = 0
    with tempfile.TemporaryDirectory(prefix="axiom-eval-") as tmp:
        for doc in lock["docs"]:
            name = doc["file"]
            target = Path(tmp) / name
            request = urllib.request.Request(
                doc["source_url"], headers={"User-Agent": USER_AGENT}
            )
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    payload = response.read()
            except Exception as exc:  # network/TLS/HTTP failure
                raise CorpusError(f"{name}: download failed: {exc}") from exc

            require(
                len(payload) == doc["bytes"],
                f"{name}: downloaded {len(payload)} bytes, expected {doc['bytes']}",
            )
            digest = hashlib.sha256(payload).hexdigest()
            require(
                digest == doc["sha256"],
                f"{name}: downloaded sha256 {digest}, expected {doc['sha256']}",
            )
            target.write_bytes(payload)
            reader = PdfReader(str(target))
            require(
                len(reader.pages) == doc["pages"],
                f"{name}: extracted {len(reader.pages)} pages, expected {doc['pages']}",
            )
            page_text[name] = [page.extract_text() or "" for page in reader.pages]
            verified += 1

    for row in rows:
        for index, gold in enumerate(row["gold"]):
            name = gold["doc"]
            page = gold["page"]
            require(name in page_text, f"{row['id']} gold[{index}]: unknown doc {name}")
            pages = page_text[name]
            require(
                1 <= page <= len(pages),
                f"{row['id']} gold[{index}]: page {page} outside {name}",
            )
            require(
                normalized_text(gold["span"]) in normalized_text(pages[page - 1]),
                f"{row['id']} gold[{index}]: span not found on {name} page {page}",
            )

    return {"downloaded": verified, "gold_spans": sum(len(row["gold"]) for row in rows)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--download",
        action="store_true",
        help="download every source PDF and verify bytes, pages, and gold spans",
    )
    args = parser.parse_args(argv)

    try:
        summary = verify_offline()
        print(
            "Frozen corpus OK: "
            f"{summary['documents']} documents, {summary['pages']} pages, "
            f"{summary['questions']} questions "
            f"({summary['outcomes'].get('answer', 0)} answer, "
            f"{summary['outcomes'].get('abstain', 0)} abstain)."
        )
        if args.download:
            downloaded = verify_downloads()
            print(
                "Sources OK: "
                f"{downloaded['downloaded']} PDFs downloaded with exact bytes, "
                f"{downloaded['gold_spans']} gold spans found on their pages."
            )
    except CorpusError as exc:
        print(f"corpus verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
