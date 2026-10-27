# Axiom — Document State Machine (Phase 1, Step 4)

Authoritative for v0.1 document lifecycle. Matches `03-erd-api.md §6` (worker contract); that section governs SQL/transaction detail, this file governs states and who may trigger each edge. Phase 4 implements the worker edges; the implementation is explained in [`../phase-4/README4.md`](../phase-4/README4.md).

## States

The upload path creates documents directly in `queued`: the bytes are durable before the row is committed. Phase 4 then claims the row, processes it outside the claim transaction, and commits or classifies failure.

`queued → processing → retrying → indexed / failed`. `uploaded` is part of the enum and the sweeper, but the current upload path does **not** produce it; it remains a supported recovery state for a split save/enqueue writer (D-016). Terminal queryable states: `indexed`, `failed`. `processing`/`retrying` are worker-owned. No other states in v0.1.

```mermaid
stateDiagram-v2
    [*] --> queued: POST /documents (Phase 3 + 4)
    [*] -.-> uploaded: supported recovery state<br/>(not produced by the current API)
    uploaded --> queued: sweeper
    queued --> processing: worker claim
    retrying --> processing: worker claim
    processing --> retrying: transient failure<br/>and claims remain
    processing --> indexed: guarded commit
    retrying --> indexed: guarded commit
    processing --> failed: permanent failure<br/>or attempts exhausted
    retrying --> failed: permanent failure<br/>or attempts exhausted
    queued --> failed: sweeper finds attempts at cap
    retrying --> failed: sweeper finds attempts at cap
    failed --> queued: POST retry (member+)<br/>retryable only
    uploaded --> [*]: delete (admin+)
    queued --> [*]: delete (admin+)
    indexed --> [*]: delete (admin+)
    failed --> [*]: delete (admin+)
```

## Phase 4 implementation note

Phase 3's single-commit handoff was kept. `POST /documents` commits `queued` after the bytes are durable, then the route publishes `documents.process` as a best effort. A failed publish leaves a `queued` row and returns `202`; the sweeper republishes it after the queued stale threshold. A duplicate upload is not a transition at all: it returns the existing document and publishes nothing.

The `uploaded` state and the `uploaded → queued` sweeper rule remain in the implementation for a future split writer. They are not produced by the Phase 4 API. This is deliberate and recorded as D-016.

## Transition table [v0.1]

| From → To | Triggered by | Guard | Side effects |
|---|---|---|---|
| `∅ → queued` | API `POST /documents` (member+) | PDF validation, size/type/magic-byte checks pass; not a duplicate unless `allow_duplicate=true` | file stored and durable, then row committed as `queued`; `processing_attempts=0`; best-effort publish after commit |
| `∅ → uploaded` | split save/enqueue writer (not implemented) | reserved for the save-succeeded / enqueue-failed path | file/row durable, broker publish pending; sweeper recovers |
| `uploaded → queued` | Phase 4 sweeper | row is older than `SWEEPER_UPLOADED_STALE_SECONDS` and still `uploaded` | clears error and `retryable`; publishes after the transition commits |
| `queued → processing` | worker claim | conditional `UPDATE ... WHERE status IN ('queued','retrying') AND processing_attempts < max_attempts RETURNING`; else exit | `processing_attempts+1`; new `processing_runs` row opened as `claimed` |
| `retrying → processing` | worker claim | same claim guard | same as above; new run row with the next attempt number |
| `processing → retrying` | worker | transient error AND `processing_attempts < max_attempts` | error code/message set, `retryable=true`, run closed as `retrying`, fresh task published with backoff |
| `processing/retrying → indexed` | worker guarded commit | one txn: replace chunk set + conditional status update on `(id, workspace_id, claimed_attempt)`; zero rows → rollback | `indexed_at` set, error cleared, `retryable=false`, run closed as `indexed` |
| `processing/retrying → failed` | worker | permanent error OR transient at the cap | error code/message set (`ATTEMPTS_EXHAUSTED` at the cap); `retryable` reflects the error family; run closed as `failed` |
| `queued/retrying → failed` | Phase 4 sweeper | row older than its stale threshold AND `processing_attempts >= max_attempts` | `ATTEMPTS_EXHAUSTED`, `retryable=false`, `updated_at=now()`; no task published |
| `processing → failed` | Phase 4 sweeper | row older than `SWEEPER_PROCESSING_STALE_SECONDS` AND at the cap | `ATTEMPTS_EXHAUSTED`, `retryable=false`; open run closed as `timeout` |
| `failed → queued` | API `POST /documents/{id}/retry` (member+) | from `failed` AND `retryable=true`; double-click → second call sees `queued` → `409` | error cleared, `retryable=false`; **no attempt increment** and **no run row**; next claim consumes the attempt |
| `uploaded/queued/indexed/failed → deleted` | API `DELETE /documents/{id}` (admin+) | not in `processing/retrying`; row locked `FOR UPDATE` | row delete flushed, storage object removed, commit; a storage outage rolls the row back (`503`); a missing object is idempotent; chunks/runs cascade; citations keep snapshots |

## Attempt semantics

`processing_attempts` counts **worker claims**, not uploads and not retry clicks.

- Upload starts at `0`.
- The first claim sets it to `1`.
- A transient failure followed by a fresh task and a new claim sets it to `2`.
- `POST /documents/{id}/retry` does not change it. The endpoint sets `queued`; the next claim increments the counter and opens the run row.
- The claim refuses to start when `processing_attempts >= MAX_PROCESSING_ATTEMPTS` (5 by default). The fifth claim is allowed; a sixth is not.
- A manual retry of a document already at the cap can be accepted into `queued` (the endpoint only checks `failed + retryable`), but no claim can win. The sweeper then fails it as `ATTEMPTS_EXHAUSTED`. This is the cap doing its job, not a lost retry.

## Illegal transitions (all → `409 INVALID_STATE`) [v0.1]

- `POST retry` from any state except `failed`, or when `retryable=false`.
- `DELETE` while in `processing`/`retrying` (`409 CONFLICT` — worker owns the doc).
- Worker commit/claim/failure against a state or attempt it did not claim (stale worker → zero-row update → rollback, never a force-write).
- Any client-supplied `status` field: status is server-derived only; requests carrying it are `400 VALIDATION_ERROR`.

## Delete vs search visibility

Only `indexed` documents are searchable. `uploaded/queued/processing/retrying/failed` rows are never cited. After delete, citations survive as snapshots (`original_filename`, `page_number`, `passage_text`; `chunk_id` NULL) per `03-erd-api.md §3`.

Phase 4's integrity processor indexes documents with **zero chunks**. `indexed` is a lifecycle state, not yet a retrieval guarantee; Phase 5 replaces the processor and backfills those rows before retrieval can use them as evidence.

## Invariants / enforcement

- DB: `status` ENUM, `UNIQUE(document_id, chunk_index)`, `UNIQUE(document_id, attempt_no)`, composite FKs (03 §7).
- DB: `documents.checksum` plus a **non-unique** `(workspace_id, checksum)` index; deduplication is advisory, never a uniqueness guarantee.
- DB: `documents.retryable` is the persisted manual-retry permission; `ATTEMPTS_EXHAUSTED` and permanent-final failures set it to `false`.
- Server: transition allow-list above; role checks (retry member+, delete admin+).
- Server: `processing_attempts` is incremented only by the claim; the retry endpoint and sweeper do not increment it.
- Server: one `processing_runs` row per claim; claim opens it, commit/failure/timeout closes it, manual retry creates none.
- Storage: the object key is server-generated (`uploads/{document_id}/original.pdf`); the user filename never participates in a path.
- Txn: claim + guarded commit + sweeper recovery (03 §6).
- Txn: upload writes bytes before the `queued` row commit and compensates a failed commit; delete flushes the row, removes the object, then commits.
- Tests: 03 §9 items 5–9 (deletion race, duplicates, retry idempotency, sweeper recovery, crash recovery), plus the Phase 4 claim/commit/sweeper suites listed in [`../phase-4/README4.md`](../phase-4/README4.md) §13.
