# Axiom — Phase 5 (PDF RAG MVP): Baseline and Decisions

> **Status:** Step 1 (baseline and decisions) is complete. Phase 5 started from
> the Phase 4 HEAD `532d7f7` on branch `phase-5-pdf-rag`. No Phase 5 application
> code or migration has been written yet; the Alembic head is still `0002`.
> The Phase 4 suite remains the regression baseline (503 tests).
>
> This document records the decisions the implementation must follow. The
> append-only ADR entries are **D-025** through **D-029** in
> [`docs/phase-1/07-decision-log.md`](../phase-1/07-decision-log.md).

---

## 1. Confirmed baseline

| Area | Decision |
|---|---|
| Provider boundary | Server-side, provider-neutral **OpenAI-compatible** HTTP adapters for embeddings and generation. Tests and CI use deterministic fake adapters; no test calls a real provider. |
| Embeddings | Reference model **`text-embedding-3-small`** with **`dimensions=768`**. `EMBEDDING_DIM=768` and the existing `VECTOR(768)` column are unchanged. Similarity is cosine. |
| Generation | Reference model **`gpt-4o-mini`**, configurable. The model receives only retrieved chunks and returns a strict structured decision. |
| Trace | Add `messages.retrieval_trace` as **`JSONB NULL`** in migration `0003_phase5_rag.py`, schema version **`retrieval-trace-v1`**. |
| Chunking | Page-aware **`pdf-text-v1`**: roughly **900–1,100 characters**, about **150 characters of overlap**, **no chunk crosses a page boundary**. |
| Abstention | Two-stage: a retrieval **score floor** (`RAG_MIN_SCORE=0.30` by default), then a **structured LLM decision**, then **citation validation before persistence**. |
| Evaluation corpus | **`eval/v0.1` is frozen**: 4 PDFs, 87 pages, 20 questions (15 answer, 5 abstain). Its manifest and verifier are committed; the PDFs are downloaded and hash-checked at evaluation time. |

The implementation may add configuration fields and modules, but it may not
silently change these decisions. A change requires a new ADR that supersedes
the earlier entry and, for the corpus, a new `eval/v0.2`.

---

## 2. Provider and model rules

### 2.1 Adapter boundary

- Application code depends on ports, not on a provider SDK. The HTTP adapter
  implements the OpenAI-compatible embeddings and chat-completions shapes.
- The API key is read from the environment (`.env` for host runs, Compose
  environment for containers). It is never logged, persisted, or returned in
  an error envelope.
- Provider errors are classified like storage errors: a timeout, connection
  failure, `429`, or `5xx` is transient; an authentication, malformed-request,
  or unsupported-model error is permanent. The document lifecycle and the chat
  request must not record a provider body as a client-facing message.
- Tests inject fakes. A deterministic fake embedder returns a fixed vector for
  a known text; a fake generator returns a scripted structured decision. CI is
  offline by design.

### 2.2 Embedding identity

Every chunk records:

```text
embedding_model = "text-embedding-3-small"
embedding_dim   = 768
```

The pair is part of the chunk identity. Indexing rejects a vector whose model
or dimension does not match the configured values. Query-time retrieval uses
the same pair and rejects a mismatch rather than comparing vectors from
different spaces. `chunks.embedding_model` exists precisely so a model change
is visible instead of silently mixing incompatible vectors.

`text-embedding-3-small` is the reference because it supports a 768-dimensional
projection, keeping the existing `VECTOR(768)` schema valid. `EMBEDDING_MODEL`
and `EMBEDDING_DIM` are configuration, not constants; a different
OpenAI-compatible provider must expose a 768-dimensional model to use the
current schema without a migration.

### 2.3 Generation identity

The reference generation model is `gpt-4o-mini`. It is deliberately a small,
low-latency model because the MVP's quality question is grounding and citation
discipline, not open-ended reasoning. `LLM_MODEL` is configurable; the model
name is stored on the assistant message and used by the evaluation harness.

The generation call is bounded by a timeout and a maximum output size. A
provider failure produces a controlled error or an abstention, never a partial
answer without a trace.

---

## 3. Retrieval trace schema

### 3.1 Storage

Migration `0003_phase5_rag.py` adds:

```sql
ALTER TABLE messages ADD COLUMN retrieval_trace JSONB NULL;
```

- Nullable: user messages and assistant messages written before Phase 5 have no
  trace.
- Written only on assistant messages that completed a retrieval/generation
  path (including an abstention).
- No JSONB index in v0.1. The trace is written once and read with its message;
  there is no query pattern that filters inside it yet.
- No backfill. Historical assistant messages remain `NULL` rather than
  pretending a trace existed.
- Downgrade drops the column. Citations remain relational snapshots and are not
  moved into the trace.

### 3.2 `retrieval-trace-v1`

```json
{
  "schema_version": "retrieval-trace-v1",
  "query": "What BLEU score did the Transformer achieve?",
  "top_k": 8,
  "min_score": 0.30,
  "embedding_model": "text-embedding-3-small",
  "embedding_dim": 768,
  "candidates_considered": 24,
  "retrieval_ms": 42,
  "retrieved": [
    {
      "chunk_id": "00000000-0000-0000-0000-000000000000",
      "document_id": "00000000-0000-0000-0000-000000000000",
      "page_number": 1,
      "score": 0.82
    }
  ],
  "best_score": 0.82,
  "decision": "answer",
  "abstain_reason": null,
  "missing_evidence": [],
  "citation_validation": {
    "attempts": 1,
    "checked": 1,
    "valid": true,
    "failure": null
  }
}
```

Rules:

- `decision` is `"answer"` or `"abstain"`. The trace is the auditable record of
  which branch was taken.
- `abstain_reason` is one of `"below_score_floor"`, `"llm_abstained"`,
  `"citation_validation_failed"`, or `null` when the decision is an answer.
- `retrieved` contains only chunks that survived the workspace, `indexed`
  status, and score filters. It carries IDs and scores, not passage text;
  citation snapshots hold the text.
- `missing_evidence` is non-empty for an abstention and empty for an answer.
- `citation_validation.attempts` counts the structured-output attempt(s),
  including the one repair attempt. `failure` is a stable code, not a provider
  message.
- `messages.prompt_version`, `messages.model`, `messages.latency_ms`, and
  `messages.tokens` remain the authoritative scalar generation metadata. The
  trace stores the retrieval and decision context; it does not replace those
  columns.
- The trace never contains an API key, a raw prompt, a provider response body,
  or a full document. It may contain the user's query because the query is the
  input being audited (US-03).

---

## 4. Page-aware chunking (`pdf-text-v1`)

### 4.1 Extraction

- Extract text page by page with `pypdf`.
- Normalize line endings and whitespace runs, but preserve paragraph breaks
  where possible.
- Skip pages with no extractable text. If the whole document yields no text,
  fail processing with `NO_EXTRACTABLE_TEXT` (permanent-final) rather than
  indexing an empty, silently unsearchable document.
- Page numbers are 1-based and come from the PDF page, not from a text offset.

### 4.2 Boundaries and size

- Target **900–1,100 characters** per chunk; **1,100 is a hard maximum**.
- Prefer a paragraph boundary, then a sentence boundary, then a word boundary.
  Hard-split only when a single sentence/word run exceeds the maximum.
- Apply about **150 characters of overlap** at a word boundary, inside the same
  page. Overlap exists to preserve context at a split, not to duplicate whole
  paragraphs.
- **No chunk crosses a page boundary.** A page therefore has one or more
  chunks, each with that page's number. This is what makes a citation's
  `page_number` exact.
- A page tail shorter than 300 characters is merged into the previous chunk
  when the result stays within 1,100 characters; otherwise it is emitted as its
  own chunk. A short page is never padded or merged across pages.

### 4.3 Identity and versioning

- `chunk_index` is zero-based and global to the document, in page order.
- `page_number` is the page on which the chunk starts.
- `source_type = "text"` for this v0.1 PDF path.
- `chunk_version = "pdf-text-v1"`.
- Determinism: the same bytes and the same version produce the same chunk set.
  Changing a boundary, overlap, normalization, or merge rule requires a new
  version string.
- The existing guarded commit already replaces the whole chunk set for a
  document, so a re-index cannot leave a mixed-version set visible.

### 4.4 Embedding and commit

- Embed chunks in bounded batches. A batch failure aborts the processing
  attempt; the guarded commit writes nothing.
- Every chunk in one document commit uses the configured model and dimension.
  A provider that returns a different dimension is a permanent error, not a
  reason to resize the column.
- The processor returns `NewChunk` values through the existing
  `DocumentProcessor` port. The worker keeps ownership of the claim, attempt
  fence, transaction, and run row.

---

## 5. Abstention and citation validation

### 5.1 Retrieval stage

- Scope the query to the caller's server-side `workspace_id` and to documents
  with `status='indexed'` that have chunks. Never retrieve from `queued`,
  `processing`, `retrying`, or `failed` documents.
- Use exact cosine search over the workspace-filtered chunk set. `RAG_TOP_K`
  defaults to **8** and is capped at **20**.
- If no candidate exists, or the best score is below **`RAG_MIN_SCORE=0.30`**,
  abstain immediately with `abstain_reason="below_score_floor"`. No generation
  call is made.
- The floor is a guardrail against spending a generation call on obviously
  unrelated text. It is not the sole decision: the model must still decide
  whether the retrieved evidence actually supports an answer.

The 0.30 value is an initial default for `text-embedding-3-small`. It is
calibrated on `eval/v0.1` during Phase 6; changing it is a configuration change,
not a schema change. A different embedding model requires recalibration because
cosine score distributions are not comparable across models.

### 5.2 Structured generation stage

The prompt receives only the retrieved chunks, each with a stable citation
label. It requests strict JSON:

```json
{
  "decision": "answer",
  "answer": "A grounded answer.",
  "citations": ["chunk-id-1"],
  "missing_evidence": []
}
```

or:

```json
{
  "decision": "abstain",
  "answer": null,
  "citations": [],
  "missing_evidence": ["The corpus does not state the training cost in dollars."]
}
```

Instructions inside a PDF are evidence, not commands. The prompt says so, and
the output validator never executes text from a chunk.

### 5.3 Validation before persistence

An `answer` decision is accepted only when all of the following hold:

1. the answer is non-empty and contains no fabricated citation label;
2. at least one citation is returned;
3. every cited chunk was in **this request's** retrieved set;
4. every cited chunk belongs to the caller's workspace and an `indexed`
   document;
5. `document_id`, `original_filename`, `page_number`, and `passage_text` are
   copied **server-side** from the chunk/document, never from model output;
6. `score` is the retrieval score for that chunk, not a model-produced number.

If structured output is malformed or any citation fails validation, the
pipeline makes **one repair attempt** with the validation error. If it fails
again, the request abstains with `abstain_reason="citation_validation_failed"`.
No assistant answer and no citation rows are persisted for a failed answer.

An `abstain` decision persists an assistant message with the abstention text,
a non-empty `missing_evidence`, a trace, and **zero citations**. A response can
never contain a citation that was not in the retrieved set.

### 5.4 Persistence

- The assistant message, its citations, and its trace are written in one
  transaction.
- Retrieval happens before that transaction; a provider timeout cannot leave a
  half-written message.
- `citations.chunk_id` remains `ON DELETE SET NULL`; the snapshot columns keep
  the evidence readable after a document is deleted.
- A trace is written for both branches, so evaluation can distinguish a correct
  abstention from a missing trace.

---

## 6. Frozen evaluation corpus

`eval/v0.1` is frozen. The full contract is in
[`eval/v0.1/FREEZE.md`](../../eval/v0.1/FREEZE.md) and the hashes are in
[`eval/v0.1/MANIFEST.sha256`](../../eval/v0.1/MANIFEST.sha256).

| Property | Value |
|---|---|
| Documents | 4 PDFs: `attention.pdf`, `bert.pdf`, `udhr_en.pdf`, `nist-ai-rmf.pdf` |
| Pages | 87 |
| Questions | 20 (`q01`–`q20`) |
| Outcomes | 15 answer, 5 abstain |
| Categories | 7 fact, 4 multi-chunk, 3 cross-doc, 1 citation, 3 unanswerable, 2 isolation |
| Workspaces | 18 `ws-a`, 2 `ws-b` isolation rows |
| Gold spans | 22 verbatim spans, each checked against its recorded page |

Verification:

```powershell
# Offline: manifest, lock, schema, row contract, distribution
python eval/v0.1/verify.py

# Live: exact source bytes/hashes/pages and gold-span page placement
python eval/v0.1/verify.py --download
```

The PDFs are not committed. The evaluation harness downloads each source and
aborts on any byte, hash, or page mismatch. Editing a question, a gold span, the
schema, or a source lock requires a new corpus version; it is never an in-place
edit.

---

## 7. Step 1 exit checks

| Check | Result |
|---|---|
| Branch starts from Phase 4 HEAD `532d7f7` | `phase-5-pdf-rag` created from that commit |
| ADRs recorded | D-025 through D-029 appended to the decision log |
| Provider / model / dimension confirmed | OpenAI-compatible adapter; `text-embedding-3-small`, 768; `gpt-4o-mini` reference |
| Trace schema confirmed | `messages.retrieval_trace JSONB NULL`, `retrieval-trace-v1`, migration `0003` planned |
| Chunking confirmed | `pdf-text-v1`, 900–1,100 characters, ~150 overlap, page-bounded |
| Abstention confirmed | 0.30 score floor, structured decision, server-side citation validation, one repair then abstain |
| Corpus frozen | `eval/v0.1` manifest + offline/live verifier; sources re-verified 2026-10-27 |
| Phase 4 baseline green | 503 tests and migration head `0002` unchanged |
| Application implementation started | No — Step 1 is decisions and corpus freeze only |

The next step is configuration and provider ports, followed by PDF extraction,
page-aware chunking, embeddings, and the zero-chunk backfill. Migration `0003`
and the chat API land later in the phase. None of that is part of this
baseline.
