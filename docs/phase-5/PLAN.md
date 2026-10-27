# Axiom — Phase 5 delivery plan

**Status:** Step 1 complete. Steps 2–12 are the implementation sequence; no step
may skip its exit criteria or weaken an assertion to make a gate pass.

| # | Step | Main work | Exit criteria |
|---|---|---|---|
| 1 | Baseline and decisions | Confirm provider, 768 dimension, trace schema, chunking, and abstention rules. Record decisions in the decision log. Freeze the `eval/v0.1` corpus. | Branch is clean; ADRs recorded; Phase 4 baseline remains green. **Done.** |
| 2 | Configuration and provider ports | Add `EmbeddingProvider` and `Generator` protocols, provider factories, fake adapters, config fields, `.env.example` entries, and provider-specific error types. | App boots with fake providers; invalid model/dimension/config fails fast; no network in tests. |
| 3 | PDF extraction | Add `pypdf`, page-level extraction, checksum/magic-byte verification, encrypted/corrupt/no-text detection, page and character limits. | Synthetic and real PDFs produce correct page text; every failure maps to a stable error code. |
| 4 | Page-aware chunking | Implement deterministic paragraph/sentence splitting, overlap, hard character bounds, whitespace normalization, and metadata (`page_number`, `chunk_version`, `source_type`). | Chunks are deterministic, non-empty, page-scoped, and cover the source text without runaway overlap. |
| 5 | Embeddings and PDF processor | Batch chunk embeddings, validate vector length and finite values, compose extraction + chunking + embedding into `PdfProcessor`, return `ProcessResult`. | Processor emits 768-dimensional chunks; provider failure produces no partial output; checksum remains verified. |
| 6 | Worker integration and failure taxonomy | Switch `get_processor()` to `PdfProcessor`, cache the embedder per worker process, add extraction/embedding error classes, update logs and `worker_smoke.py`. | Upload reaches `indexed` with chunks and pages; retries replace chunks atomically; corrupt/no-text/provider failures are classified correctly. |
| 7 | Zero-chunk backfill | Add `scripts/backfill_zero_chunk_docs.py` with `--dry-run`/`--apply`, batching, advisory lock, old run cleanup, attempt reset, queue transition, publish, and structured logs. | Dry-run changes nothing; apply requeues every zero-chunk indexed document; rerun is a no-op; publish failure is recovered by the sweeper. |
| 8 | Retrieval service | Query embedding, exact cosine SQL, workspace/indexed/model/dimension filters, deterministic ordering, `top_k`, score threshold, evidence DTOs. | Known-vector tests return correct nearest neighbors; queued/failed/deleted/foreign chunks never appear. |
| 9 | Generation, citations, abstention | Prompt versioning, evidence delimiters, strict JSON output, one bounded repair attempt, citation subset validation, snapshot persistence, injection resistance, abstention mapping. | Every citation refers to a retrieved chunk; invalid citations never persist; unanswerable questions abstain with `missing_evidence`; injected instructions are treated as evidence, not executed. |
| 10 | Chat API and trace migration | Add migration `0003`, `POST /chat`, `GET /conversations/{id}`, `GET /conversations/{id}/messages`, schemas, role checks, conversation ownership, message/citation trace persistence, delete/re-index race handling. | Answer and abstention responses are persisted and readable; cross-workspace access is `404`; admin/owner/private rules hold; deleted evidence cannot become a new citation. |
| 11 | Evaluation harness | Download and verify the frozen corpus by SHA-256, seed workspaces, upload documents, wait for indexing, run the 20 seed questions, compute retrieval/citation/abstention metrics, and write a reproducible report. | Baseline report exists; metrics are recorded; forbidden documents are zero; failures are visible rather than hidden. |
| 12 | Runtime, CI, docs, final drills | Add Compose variables, CI fake-provider path, migration checks, chunk/retrieval smoke tests, expand `docs/phase-5/README5.md`, update README status, and record failure drills. | Full suite, migration round-trip, Compose smoke, evaluation baseline, and GitHub Actions are green; worktree is clean. |

The confirmed Step 1 decisions are in
[`README5.md`](README5.md) and ADRs D-025–D-029 in
[`../phase-1/07-decision-log.md`](../phase-1/07-decision-log.md).
