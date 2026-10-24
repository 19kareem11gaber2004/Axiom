# Axiom v0.1 — User Stories (Phase 1, Step 1)

Scope: PDF-only text RAG MVP (end of Phase 5). Workspace-scoped auth. Async indexing via Celery + Redis. Exact vector search. No Solution Advisor yet.

## Roles

- Knowledge worker: uploads docs, asks questions.
- Reviewer: inspects evidence (read-only in v0.1; approve/reject comes in Phase 8).

## US-01: Upload PDF for background indexing

**As a** knowledge worker, **I want** to upload a PDF to my workspace **so that** it can be indexed without blocking my request.

Acceptance:
- `POST /api/v1/documents` accepts PDF only, enforces size limit, validates extension + magic bytes, stores via a server-generated key, and deduplicates by workspace-scoped SHA-256.
- Returns `202 Accepted` with `document_id` and `status: queued` for new content; returns `200 OK` with `duplicate: true` and the existing document when the same bytes already exist in the workspace. `allow_duplicate=true` forces a new document.
- Record persists across API restart (`GET /api/v1/documents/{id}` returns metadata).
- Maps to FR-01, FR-02.

## US-02: Track processing status and retry recoverable failures

**As a** knowledge worker, **I want** to see processing status and retry failures **so that** I know when a document is searchable.

Acceptance:
- `GET /api/v1/documents` / `GET /api/v1/documents/{id}` expose `queued → processing → retrying → indexed / failed`. Phase 3 creates documents in `queued`; `uploaded` is reserved for the Phase 4 save-succeeded / enqueue-failed recovery path.
- `failed` includes actionable `error_code` + `message`, no internal stack traces.
- `POST /api/v1/documents/{id}/retry` works only from `failed` (retryable) and never duplicates chunks.
- Maps to FR-03, FR-10.

## US-03: Ask a question over indexed workspace documents

**As a** knowledge worker, **I want** to ask a natural-language question **so that** I get an answer from my workspace's indexed PDFs only.

Acceptance:
- `POST /api/v1/chat` validates input, scopes retrieval by server-side `workspace_id`.
- Queued/failed documents are never cited.
- Per-answer trace stored: query, chunk IDs + scores, prompt version, model, latency, tokens.
- Maps to FR-06.

## US-04: Receive grounded answer with verifiable citations

**As a** knowledge worker, **I want** citations pointing to document + page + passage **so that** I can verify every claim.

Acceptance:
- Every factual claim cites at least one retrieved chunk actually returned by retrieval.
- Citation stores `document_id`, `page_number`, passage text snapshot (survives re-index/delete of vectors).
- Clicking citation opens source evidence.
- Injection fixture: instructions embedded in a PDF are treated as evidence, never executed.
- Maps to FR-07, FR-11.

## US-05: Honest abstention when evidence is insufficient

**As a** system, **I want** to abstain instead of hallucinating **so that** users are not misled.

Acceptance:
- When top-k evidence does not support an answer, return explicit "not enough evidence" + what is missing.
- No fabricated citations; structured-output validation passes.
- Measured on eval set (abstention precision/recall on unanswerable subset).
- Maps to FR-07, FR-12.

## US-06: Workspace isolation enforced server-side

**As a** workspace member, **I want** my documents and answers isolated **so that** another workspace cannot list, retrieve, download, or cite them.

Acceptance:
- All queries filter by membership-derived `workspace_id`; client-supplied IDs never grant access.
- Cross-workspace list/get/download/chat/citation attempts return 403/404 without leaking existence.
- Covered by automated auth tests (Phase 2) + retrieval isolation test (Phase 5).
- Maps to FR-06, FR-11, NFR security.

## US-07: Delete a document and its derived data

**As a** knowledge worker, **I want** to delete a document **so that** its file, chunks, and vectors are removed.

Acceptance:
- `DELETE /api/v1/documents/{id}` removes original file + cascades chunks/vectors, writes audit log.
- Blocked or cancels safely while `processing`; never leaves orphan vectors.
- Saved citations keep their passage-text snapshot for audit.
- Maps to FR-03, FR-11.

## Non-goals for v0.1

- No model training / fine-tuning.
- No autonomous business decisions (Advisor, approvals come in Phase 8).
- No microservices split; modular monolith only.
- No enterprise SSO (JWT + workspace roles only).
- No DOCX, OCR, images, audio, video (Phase 7).
- No hybrid search, reranker, HNSW (Phase 6, only if measured).
- No polished UI beyond login / upload+status / ask+citations.

## Traceability

| Story | FRs |
|---|---|
| US-01 | FR-01, FR-02 |
| US-02 | FR-03, FR-10 |
| US-03 | FR-06 |
| US-04 | FR-07, FR-11 |
| US-05 | FR-07, FR-12 |
| US-06 | FR-06, FR-11 |
| US-07 | FR-03, FR-11 |
