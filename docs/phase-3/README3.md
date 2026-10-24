# Axiom — Understanding Phase 3 (Document Ingestion)

> **Who this is for.** You have the project and you want to *understand* it, not just run it. This document explains what Phase 3 built, why each piece exists, and how to read the code.
>
> **How to use it.** Read §1–§3 first (the story). Then follow the upload path in §4 and the reading order in §10. Every section names the file and function it describes. When you want to *prove* that you understand it, work through the exercises in §11.
>
> The operational README stays in `README.md` (how to install and run). This one is about *why*.

---

## 1. The one-sentence version

Phase 3 turned `POST /documents` from "write a database row and hope a file exists" into **a durable, workspace-scoped ingestion boundary that returns a document only after its bytes and metadata agree.**

---

## 2. The problem Phase 3 solves

A document upload crosses two systems that cannot participate in one atomic transaction:

1. a filesystem (or, later, an object store) holds the bytes;
2. PostgreSQL holds the metadata and lifecycle state.

That split creates four concrete failure modes:

- **A row without a file.** If the row is committed first and the file write fails, the API advertises a document that the worker cannot read. Download fails, and the row is stuck in a state no component can repair.
- **A file without a row.** If the file is written first and the database commit fails, an unreferenced object remains. It costs storage and cannot be found through the API.
- **Duplicate work.** A client retry, a double-click, or an uncertain network response can upload the same bytes twice. Two rows and two files mean duplicate storage and, once Phase 4 exists, duplicate processing.
- **Identity confusion.** The filename is user-controlled, unstable, and not proof of content. If it were the identity, `report.pdf` would collide with a different `report.pdf`, while the same PDF under `report-final.pdf` would look new.

Phase 3 makes the common path safe and the failure path explicit:

- bytes are written through a storage port before the row is committed;
- the content's SHA-256 is the deduplication key, scoped to one workspace;
- a duplicate is a successful lookup (`200`), not an error;
- intentional re-uploads remain possible;
- a storage outage is reported as a retryable `503`, not as a validation error.

Phase 3 does **not** process documents. It ends with a durable `queued` row and a file the worker can read.

---

## 3. What changed, concretely

| Before Phase 3 | After Phase 3 |
|---|---|
| Upload code owned the local filesystem path | Routes and the ingestion service depend on the `FileStorage` port |
| A duplicate created another row and file | Workspace-scoped SHA-256 dedup returns the existing document |
| Every upload answered `202` | New uploads answer `202`; duplicates answer `200` with `duplicate: true` |
| Forced re-upload had no explicit contract | `?allow_duplicate=true` creates a new document intentionally |
| Storage errors were local implementation details | Storage I/O failures map to `503 DEPENDENCY_UNAVAILABLE` |
| The worker's access to files was assumed | Compose gives the API and worker the same bind-mounted `STORAGE_ROOT` |
| The database and file store could silently diverge | File-before-row ordering plus best-effort compensation is documented and tested |
| No migration was needed | The existing `documents.checksum` column and non-unique `(workspace_id, checksum)` index are sufficient |

There is **no Alembic migration in Phase 3**. That is deliberate: the Phase 1 schema already contains the checksum and index the feature needs. A migration here would be evidence that the original design was insufficient.

---

## 4. The upload path

The happy path is deliberately ordered:

```text
POST /api/v1/documents (multipart PDF)
              |
              v
[1] read at most max_upload_bytes + 1
              |
              v
[2] normalise filename + validate PDF
              |
              v
[3] SHA-256 the bytes
              |
              v
[4] duplicate in this workspace? ---- yes ----> 200 existing document
              |                                  (no file, no row, no commit)
              | no, or allow_duplicate=true
              v
[5] generate document id + storage key
              |
              v
[6] storage.save(key, bytes)  <-- bytes durable here
              |
              v
[7] INSERT document (status="queued") + COMMIT
              |
              v
[8] refresh and return 202 (or 500 if refresh itself fails)
```

Two details matter more than the arrows:

- **Validation happens before any side effect.** A rejected upload does not create a temporary file, a row, or a storage directory.
- **The file is written before the row is committed.** A committed row therefore points at bytes that were already durable. The reverse ordering would allow a committed row whose file never arrived.

The upload endpoint accepts an optional boolean query parameter:

```text
POST /api/v1/documents?allow_duplicate=true
```

The default is `false`. The route itself is thin: it maps multipart input and domain errors; `ingest_upload()` in `app/ingestion/service.py` owns the sequence above.

---

## 5. The storage port and the local adapter

### 5.1 The port

`app/storage/base.py` defines a small structural `Protocol` named `FileStorage`:

| Operation | Contract |
|---|---|
| `save(key, content)` | Write or replace an object. Implementations should write atomically. |
| `read(key)` | Return the complete object, or raise `StorageNotFoundError`. |
| `delete(key)` | Return `True` if removed and `False` if already absent. Deletion is idempotent. |
| `exists(key)` | Return whether the object exists. |
| `size(key)` | Return the object's size in bytes, or raise `StorageNotFoundError`. |

The application depends on this interface, not on `pathlib`, `open`, or `shutil`. That is what allows an object-store adapter to be added later without touching the upload route, the download route, or the worker.

The port has three failure types:

| Error | Meaning | API treatment |
|---|---|---|
| `StorageUnavailableError` | The backend could not complete the operation: connection failure, permissions, full disk, transient I/O. | `503 DEPENDENCY_UNAVAILABLE`, `retryable: true` |
| `StorageNotFoundError` | The requested object does not exist. | A missing download is `409 INVALID_STATE`; delete treats absence as success |
| `StoragePathError` | The key is invalid or escapes the root. This is a programming error, never client input. | `500` through the generic error boundary |

### 5.2 Keys are server-generated

`app/storage/keys.py` fixes the layout:

```text
<storage_root>/uploads/{document_id}/original.pdf
```

The user's filename never appears in the key. It is normalised and sanitised for display, then stored as `original_filename` metadata. A filename such as `../../etc/passwd.pdf` cannot influence the path because the path is built only from the server-generated document id.

The key is a POSIX-style relative path. `validate_storage_key()` rejects absolute paths, `..` components, backslashes, and drive letters. The local adapter performs a second containment check after resolving the path, including symlinks.

### 5.3 The local adapter

`app/storage/local.py` implements `LocalFileStorage`:

- A relative `STORAGE_ROOT` is resolved against the `backend/` directory, not the process working directory. Starting uvicorn from the repository root and from `backend/` therefore uses the same storage root.
- `save()` writes to a temporary file in the destination directory, flushes and `fsync`s it, then uses `os.replace()` to move it into place. A reader sees either the old object or the complete new object, never a partial upload.
- The temporary file is chmodded to `0644` so the worker, which runs as a different uid in Compose, can read it. (`mkstemp` would otherwise create it as `0600`.)
- `delete()` removes the file and prunes empty per-document directories up to, but never including, the storage root. A missing file returns `False`, so a retried delete is safe.
- `read()` and `size()` distinguish missing objects from backend failures.

`app/storage/__init__.py` exposes `get_storage()`. It reads `STORAGE_BACKEND` and constructs the local adapter. An unknown backend is rejected by configuration validation at startup, and `get_storage()` refuses it as a second line of defence.

### 5.4 Configuration and shared visibility

The default configuration is:

```text
STORAGE_BACKEND=local
STORAGE_ROOT=storage
```

In Compose, both the API and the worker receive:

```text
STORAGE_ROOT=/srv/backend/storage
```

and both mount the same host directory:

```text
./backend/storage  ->  /srv/backend/storage
```

This is a **bind mount**, not a named volume. It is what lets the worker read the exact bytes the API wrote. The directory is excluded from the Docker build context by `backend/.dockerignore`, so uploads are never copied into the image. `docker compose down -v` removes named volumes but does **not** remove bind-mounted uploads; deleting `backend/storage` is a separate, explicit step.

A production deployment on multiple hosts would need an object store or a shared filesystem. The local bind mount proves the interface and the worker handoff, not a production storage architecture.

---

## 6. Content identity vs filename identity

Axiom identifies a document's content by the **SHA-256 digest of the bytes**, not by its name.

| Scenario | Result |
|---|---|
| `report.pdf` and `copy.pdf`, same bytes, same workspace | Duplicate; the existing document is returned |
| `report.pdf` twice, different bytes | New document; the filename is only metadata |
| Same bytes in two different workspaces | Two documents; dedup never crosses a workspace boundary |
| Same bytes with `allow_duplicate=true` | New document and new file, by explicit request |
| Same bytes, existing document is `failed` | Duplicate; the existing failed document is returned |

Why the filename is a poor identity:

- it is not unique and can be changed without changing the content;
- it is user-controlled and may contain path separators, control characters, or misleading extensions;
- the same content is commonly uploaded under different names;
- using it would make deduplication either too aggressive or too weak.

The checksum is stored in `documents.checksum` as a 64-character lowercase hex digest. The `(workspace_id, checksum)` index is **non-unique**; it accelerates the lookup without forbidding intentional copies.

---

## 7. Duplicate uploads are not errors

A duplicate means "this workspace already has these bytes." The request succeeded; it simply did not create new work.

| Case | HTTP | Response | What happened |
|---|---|---|---|
| New content | `202` | `{document_id, status: "queued", duplicate: false}` | File stored, row committed as `queued` |
| Duplicate in this workspace | `200` | `{document_id: existing_id, status: existing_status, duplicate: true}` | No new file, no new row, no new commit |
| Duplicate with `allow_duplicate=true` | `202` | `{document_id: new_id, status: "queued", duplicate: false}` | A second document and file were created intentionally |
| Duplicate in another workspace | `202` | New document | The other workspace's id is never returned |
| Existing match is `failed` | `200` | `{document_id, status: "failed", duplicate: true}` | The caller may retry it or force a new upload |
| Several matches | `200` | The healthiest match | Priority below |

The duplicate response deliberately **does not modify the existing document**. Its filename, uploader, timestamps, status, and error fields stay exactly as they were. A duplicate is a lookup, not a write.

### 7.1 Which duplicate is returned?

`find_duplicate()` in `app/ingestion/service.py` considers every status and chooses the healthiest match:

1. `indexed`
2. `queued`, `processing`, or `retrying`
3. `uploaded`
4. `failed`

Within the same tier, the newest row wins. The ordering is implemented with a SQL `CASE` expression, not by filtering out unhealthy rows. A retry of the same bytes therefore returns the original row instead of creating a second one, even when the original failed.

`uploaded` is not produced by Phase 3. It remains in the priority list because Phase 4 will introduce the save-succeeded / enqueue-failed recovery state.

### 7.2 Advisory, not a uniqueness guarantee

The deduplication check is a **check-then-insert**, not a database uniqueness constraint:

```text
SELECT best match
    if found: return it
    otherwise: save file and insert row
```

Two concurrent requests with the same bytes can both pass the `SELECT` before either inserts. Both may then create a document. That is accepted for v0.1:

- a unique index on `(workspace_id, checksum)` would reject legitimate re-uploads and `allow_duplicate=true`;
- the race is rare in normal client behavior;
- advisory dedup prevents the common duplicate without turning a content hash into a permanent identity.

If strict single-instance semantics are needed later, the design should use a deliberate idempotency key or a partial unique index with an explicit conflict path, not an accidental constraint.

---

## 8. File and database ordering, compensation, and crash windows

### 8.1 Upload: file first, then row

`ingest_upload()` follows this order:

1. read at most `max_upload_bytes + 1` bytes;
2. normalise the filename and validate the PDF;
3. compute the checksum;
4. look for a duplicate unless `allow_duplicate=True`;
5. generate the document id and storage key;
6. call `storage.save()`;
7. insert the `Document` with `status="queued"` and commit;
8. refresh and return.

The failure behavior is part of the contract:

| Failure point | Result |
|---|---|
| Validation | `400 VALIDATION_ERROR`; no file and no row |
| Duplicate lookup finds a match | `200`; no file and no row are written |
| `storage.save()` fails | `503 DEPENDENCY_UNAVAILABLE`; no row is committed; the adapter cleans its temporary file |
| Database insert or commit fails | Roll back, best-effort delete the just-written object, and re-raise the original database error (`500`) |
| Database cleanup delete also fails | The original error is preserved; the orphan is logged for operators |
| Process dies after `save()` but before commit | An unreferenced object can remain; there is no row for the sweeper to find. A future orphan-file collector is the clean fix |
| Process dies after commit but before the response | The row and file are durable; a retry finds the duplicate and returns it |

Two intentional boundaries:

- `db.refresh()` is outside the compensation block. Once the commit succeeds, a refresh failure must not delete a file that the committed row references.
- The storage write is not part of the database transaction. The two systems cannot be made atomic by ordering alone; the ordering minimizes the dangerous window, and compensation handles exceptions rather than process death.

### 8.2 Delete: row flush, file removal, commit

`DELETE /documents/{id}` uses a different order because the row already exists:

1. resolve the document in the caller's workspace (`404` otherwise);
2. check the role (`admin+`) and the state (not `processing`/`retrying`);
3. `db.delete(document)`;
4. `db.flush()` so the row delete is visible inside the transaction;
5. `storage.delete(storage_key)`;
6. `db.commit()`.

Consequences:

- A storage outage rolls the row deletion back and returns `503`. The document remains, so the caller can retry.
- A missing storage object returns `False`; the row is still deleted and the response is `204`. A retried delete is therefore idempotent.
- If the file is removed and the commit then fails, the row remains with a missing file. A retry completes the delete; until then, a download returns `409 INVALID_STATE`.
- A path/programming error is not a storage outage. It rolls back and becomes a generic `500` through the error boundary.

The route logs whether the object was actually removed. It never exposes the storage key as a local path.

---

## 9. HTTP and error contract

| Situation | Status | Error code / response |
|---|---|---|
| New document stored and queued | `202` | `{document_id, status: "queued", duplicate: false}` |
| Duplicate returned | `200` | `{document_id, status, duplicate: true}` |
| Not a PDF, empty, oversized, bad filename | `400` | `VALIDATION_ERROR` |
| Missing or invalid token | `401` | `UNAUTHENTICATED` |
| Authenticated but insufficient role | `403` | `FORBIDDEN` |
| Document absent or in another workspace | `404` | `NOT_FOUND` |
| Download object missing, or status `uploaded` | `409` | `INVALID_STATE` |
| Delete while `processing`/`retrying` | `409` | `INVALID_STATE` |
| Storage backend unavailable | `503` | `DEPENDENCY_UNAVAILABLE`, `retryable: true` |
| Invalid storage key or unexpected server bug | `500` | Generic internal error; details are logged |

The API never returns a local filesystem path. Download is the only authorized byte path, and it streams the stored object with `Content-Disposition: inline` and `X-Content-Type-Options: nosniff`.

---

## 10. How to read the implementation

Read in this order. Each item is a small unit with a single responsibility.

1. **`backend/app/api/routes/documents.py` — `upload_document()`.** Start here for the request shape: the role dependency, `allow_duplicate`, the ingestion call, and the error mapping. Notice that the route contains no filesystem code.
2. **`backend/app/ingestion/service.py` — `ingest_upload()`.** Follow the eight-step sequence in §4. Then read `find_duplicate()` and `_DUPLICATE_STATUS_PRIORITY`.
3. **`backend/app/ingestion/validation.py`.** Read `normalise_filename()` and `validate_pdf_upload()`. These explain why a bad filename cannot influence a path and why validation happens before any write.
4. **`backend/app/storage/base.py`.** Read the `FileStorage` protocol and the error classes. This is the seam that keeps the API and worker backend-neutral.
5. **`backend/app/storage/keys.py`.** Read `storage_key_for()` and `validate_storage_key()`. The layout is server-generated; the filename is not part of it.
6. **`backend/app/storage/local.py`.** Read `LocalFileStorage.save()` for the atomic write, then `_path_for()` for the containment check.
7. **`backend/app/storage/__init__.py`.** Read `get_storage()`. It is the only place that turns configuration into an adapter.
8. **`backend/app/api/routes/documents.py` — `download_document()` and `delete_document()`.** These show the read path and the delete ordering, including the `503` rollback and the idempotent missing-object case.
9. **`backend/tests/test_storage_port.py`, `test_storage.py`, `test_ingestion_service.py`, `test_documents.py`, `test_openapi.py`.** These are the executable specification. Start with the storage contract, then the service, then the HTTP routes.

A useful mental model:

```text
route            owns HTTP status, role dependencies, error mapping
ingestion        owns validation, identity, ordering, transaction
storage port     owns the backend-neutral bytes contract
local adapter    owns the filesystem details
```

Each layer has one reason to change. A new storage backend touches the adapter, not the route. A new dedup policy touches the service, not the adapter.

---

## 11. Exercises

These are designed to turn reading into evidence. Use the running Compose stack and the authentication steps in `README.md`. Do not weaken a validation or authorization check to make an exercise pass.

### Exercise 1 — Prove the file and the row agree

1. Upload a small valid PDF.
2. Assert the response is `202`, `status == "queued"`, and `duplicate == false`.
3. Read the returned `document_id`.
4. Inspect `backend/storage/uploads/<document_id>/original.pdf`.
5. Compare the file's SHA-256 with the `checksum` column for that row.
6. Run a command inside the worker container that reads the same file.

**What this proves.** The API stored the bytes before committing the row, and the worker sees the same storage root. This is the Phase 3 exit criterion.

### Exercise 2 — A duplicate is a lookup, not an error

1. Upload the same bytes again under a different filename.
2. Expect `200`, `duplicate == true`, and the same `document_id`.
3. Confirm the original row's filename, uploader, and timestamp did not change.
4. Confirm no second file was created.

**What this proves.** Identity is content-based, and a duplicate does not create work or rewrite metadata.

### Exercise 3 — Intentional duplicates are explicit

1. Upload the same bytes with `?allow_duplicate=true`.
2. Expect `202`, a new `document_id`, and `duplicate == false`.
3. Confirm there are now two rows and two files with the same checksum.

**What this proves.** Deduplication does not make re-upload impossible; it makes it a deliberate request.

### Exercise 4 — Filename is metadata

1. Upload a PDF named `report.pdf`.
2. Upload different bytes with the same name.
3. Expect a new `202` and a new document.

**What this proves.** The filename is not the identity. The content hash is.

### Exercise 5 — Workspace isolation

1. Upload the same bytes in a second workspace.
2. Expect a new `202` with a new id.
3. Try to download the first workspace's document using the second workspace's credentials.
4. Expect `404`, not `403`.

**What this proves.** Deduplication is workspace-scoped, and the API never leaks the existence of another workspace's document.

### Exercise 6 — A storage outage is retryable

Run the focused route test that replaces the storage dependency with a failing adapter:

```powershell
docker compose run --rm api sh -c "pip install --no-cache-dir -r requirements-dev.txt && python -m pytest -q tests/test_documents.py -k storage"
```

Then read the assertions. The important ones are:

- the response is `503 DEPENDENCY_UNAVAILABLE`;
- the document row was not committed;
- no file was left behind when the failure happened before the write;
- the error message does not expose internal paths.

**What this proves.** A valid request that cannot be stored is a dependency failure, not a validation failure, and it is safe to retry.

### Exercise 7 — Delete is idempotent at the storage layer

1. Delete an uploaded document.
2. Expect `204`; confirm the row and file are gone.
3. Delete the same id again; expect `404`.
4. Read `LocalFileStorage.delete()` and explain why a missing object returns `False` instead of raising.

**What this proves.** The route's commit and the adapter's missing-object semantics make retries safe.

### Exercise 8 — Find the crash window

Trace what happens if the process is killed at each arrow in the §4 diagram. You should be able to state which points leave:

- no row and no file;
- a file with no row;
- a row with a durable file.

There is no way to make the two systems perfectly atomic with this design. The point of the exercise is to identify the remaining window and explain why Phase 3 accepts it.

---

## 12. What Phase 3 deliberately does not do

- **No broker publish.** The row is committed as `queued`, but no Celery task is sent. Phase 4 owns the queue.
- **No `uploaded` state or sweeper in the upload path.** The state remains part of the v0.1 model for Phase 4's save-succeeded / enqueue-failed recovery.
- **No PDF parsing or chunking.** The bytes are stored verbatim.
- **No object store.** `LocalFileStorage` is the only adapter. The port is the seam, not the implementation.
- **No strict deduplication.** Concurrent identical uploads can both create documents; see §7.2.
- **No orphan-file collector.** Exception paths compensate; a process crash between file write and row commit can leave an unreferenced object.
- **No migration.** The existing checksum column and non-unique index carry the feature.

---

## 13. Test map and verification

| Test module | What it locks down |
|---|---|
| `tests/test_storage_port.py` | The five-operation protocol, error taxonomy, key layout, path rules |
| `tests/test_storage.py` | Local and in-memory adapters: save/read, overwrite, empty objects, missing objects, idempotent delete, containment |
| `tests/test_ingestion_service.py` | Validation, checksum dedup, status priority, forced upload, cross-workspace isolation, compensation |
| `tests/test_documents.py` | HTTP routes: `202`/`200`, `duplicate` field, cross-workspace `404`, storage `503`, download, delete |
| `tests/test_openapi.py` | The `200` and `202` responses and the `allow_duplicate` parameter are visible to clients |

The full suite is expected to run against real PostgreSQL and Redis. Route tests use an isolated temporary `LocalFileStorage` or the in-memory fake; they never write to `backend/storage`.

```powershell
python -m compileall -q backend/app backend/tests backend/alembic
docker compose config --quiet
docker compose build
docker compose run --rm api sh -c "pip install --no-cache-dir -r requirements-dev.txt && python -m pytest -q"
```

The last command is the authoritative full-suite run because the default Compose stack does not publish PostgreSQL or Redis to the host. The GitHub Actions workflow runs the same suite against service containers.

---

## 14. Phase 4 handoff

Phase 3 ends with a document that is safe for Phase 4 to read:

- the row is committed as `queued`;
- the bytes are durable at `uploads/{document_id}/original.pdf`;
- the API and worker resolve the same storage root;
- a storage failure is distinguishable from a validation failure;
- a duplicate upload is a normal `200` and does not enqueue duplicate work.

Phase 4 can therefore add, without redesigning storage or the upload API:

1. the broker publish and the `uploaded → queued` recovery state;
2. the database claim and `processing_runs`;
3. PDF extraction and chunking;
4. retries, failure classification, and the sweeper.

The storage seam is already in place. The next phase should use `get_storage()` and `document.storage_key`; it should never construct a filesystem path from a user-supplied filename.
