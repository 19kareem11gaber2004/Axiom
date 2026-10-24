# Axiom — Document State Machine (Phase 1, Step 4)

Authoritative for v0.1 document lifecycle. Matches `03-erd-api.md §6` (worker contract); that section governs SQL/transaction detail, this file governs states and who may trigger each edge.

## States

Phase 3 creates documents directly in `queued`: the bytes are durable before the row is committed, and there is no broker call yet. The `uploaded` state is reserved for Phase 4's save-succeeded / enqueue-failed recovery path.

`uploaded → queued → processing → retrying → indexed / failed`. Terminal queryable states: `indexed`, `failed`. `processing`/`retrying` are worker-owned. No other states in v0.1.

```mermaid
stateDiagram-v2
    [*] --> queued: POST /documents (Phase 3)
    [*] -.-> uploaded: Phase 4 save/enqueue split (future)
    uploaded --> queued: enqueue / sweeper
    queued --> processing: worker claim
    retrying --> processing: worker claim
    processing --> retrying: transient fail
    processing --> indexed: guarded commit
    retrying --> indexed: guarded commit
    processing --> failed: permanent / attempts exhausted
    retrying --> failed: permanent / attempts exhausted
    failed --> queued: POST retry (member+)
    uploaded --> [* : delete (admin+)
    queued --> [* : delete (admin+)
    indexed --> [* : delete (admin+)
    failed --> [* : delete (admin+)
```

## Phase 3 implementation note

Phase 3 implements the upload API's single-commit handoff: the row is committed as `queued` only after the bytes are durable. A duplicate upload is not a transition at all: it returns the existing document and leaves its status and metadata unchanged, so the caller can retry a failed match later or force a new upload. The `uploaded` state and the `uploaded → queued` transition remain Phase 4 work; Phase 4 owns the actual broker publish and the recovery sweeper.

## Transition table [v0.1]

| From → To | Triggered by | Guard | Side effects |
|---|---|---|---|
| `∅ → queued` | API `POST /documents` (member+) | PDF validation, size/type/magic-byte checks pass; not a duplicate unless `allow_duplicate=true` | file stored and durable, then row committed as `queued`; `processing_attempts=0` |
| `∅ → uploaded` | Phase 4 upload/enqueue split (not implemented in Phase 3) | reserved for the save-succeeded / enqueue-failed path | file/row durable, broker publish pending; sweeper recovers |
| `uploaded → queued` | API enqueue / Phase 4 sweeper | row exists, task published or re-enqueued | Celery task carries `document_id` only |
| `queued → processing` | worker claim | conditional `UPDATE ... WHERE status IN ('queued','retrying') RETURNING` wins; else exit | `attempts+1`, new `processing_runs` row opened |
| `retrying → processing` | worker claim | same claim guard | same as above |
| `processing → retrying` | worker | transient error (timeout, rate-limit, provider 5xx) AND attempts < cap | run row closed with `error_code`, backoff scheduled |
| `processing/retrying → indexed` | worker guarded commit | single txn: replace chunk set + conditional status update on `(id, claimed_attempts)`; zero rows → rollback | `indexed_at` set, error cleared |
| `processing/retrying → failed` | worker | permanent error (corrupt/empty/unsupported) OR attempts exhausted | `error_code/message` set (`retryable` flag decides retry eligibility) |
| `failed → queued` | API `POST /documents/{id}/retry` (member+) | from `failed` AND `retryable=true`; double-click → second call sees `queued` → `409` | error cleared, `attempts+1`, new run row |
| `uploaded/queued/indexed/failed → deleted` | API `DELETE /documents/{id}` (admin+) | not in `processing/retrying` | row delete flushed, storage object removed, commit; a storage outage rolls the row back (`503`); a missing object is idempotent; chunks/runs cascade; citations keep snapshots |

## Illegal transitions (all → `409 INVALID_STATE`) [v0.1]

- `POST retry` from any state except `failed`, or when `retryable=false`.
- `DELETE` while in `processing`/`retrying` (`409 CONFLICT` — worker owns the doc).
- Worker commit/claim against a state it did not claim (stale worker → zero-row update → rollback, never a force-write).
- Any client-supplied `status` field: status is server-derived only; requests carrying it are `400 VALIDATION_ERROR`.

## Delete vs search visibility

Only `indexed` documents are searchable. `uploaded/queued/processing/retrying/failed` rows are never cited. After delete, citations survive as snapshots (`original_filename`, `page_number`, `passage_text`; `chunk_id` NULL) per `03-erd-api.md §3`.

## Invariants / enforcement

- DB: `status` ENUM, `UNIQUE(document_id, chunk_index)`, `UNIQUE(document_id, attempt_no)`, composite FKs (03 §7).
- DB: `documents.checksum` plus a **non-unique** `(workspace_id, checksum)` index; deduplication is advisory, never a uniqueness guarantee.
- Server: transition allow-list above; role checks (retry member+, delete admin+).
- Storage: the object key is server-generated (`uploads/{document_id}/original.pdf`); the user filename never participates in a path.
- Txn: claim + guarded commit + sweeper recovery (03 §6).
- Txn: upload writes bytes before the `queued` row commit and compensates a failed commit; delete flushes the row, removes the object, then commits.
- Tests: 03 §9 items 5–9 (deletion race, duplicates, retry idempotency, sweeper recovery, crash recovery).
