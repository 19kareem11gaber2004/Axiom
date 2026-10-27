# Axiom evaluation corpus — v0.1 (FROZEN)

**Status:** frozen on **2026-10-17**; integrity re-verified on **2026-10-27**.
**Do not edit this directory in place.** Any correction, replacement, or new
question creates `eval/v0.2/` instead. Phase 5 and Phase 6 results are only
comparable while the corpus is immutable.

## What is frozen

| Artifact | Purpose |
|---|---|
| `corpus.lock` | Source URLs, exact byte sizes, SHA-256 hashes, page counts, and license evidence for the four source PDFs |
| `schema.json` | The contract every question row must satisfy |
| `seed-questions.jsonl` | The 20 seed questions, gold spans, expected outcomes, and workspace visibility |
| `MANIFEST.sha256` | SHA-256 of the three artifacts above, with CRLF normalized to LF |

The PDFs are **not vendored**. The evaluation harness downloads each
`source_url` at run time and aborts unless the bytes, SHA-256, and page count
match `corpus.lock` exactly. This keeps the repository small and satisfies the
source licenses, which permit evaluation use but not redistribution.

## Composition

- **Documents:** 4 PDFs, 87 pages total.
  `attention.pdf` (15), `bert.pdf` (16), `udhr_en.pdf` (8),
  `nist-ai-rmf.pdf` (48).
- **Questions:** 20 — `q01` through `q20`.
- **Expected outcomes:** 15 answer, 5 abstain.
- **Categories:** 7 fact, 4 multi-chunk, 3 cross-doc, 1 citation,
  3 unanswerable, 2 isolation.
- **Workspaces:** 18 rows in `ws-a`, 2 isolation rows in `ws-b`.
- **Gold evidence:** every answerable row has at least one verbatim span on a
  visible page; every abstention row has no gold span and a non-empty
  `missing_evidence` explanation.

## Verification

Offline (standard library only; checks the manifest, lock, schema, and rows):

```powershell
python eval/v0.1/verify.py
```

Live source check (network plus `pypdf`; downloads each PDF and verifies exact
bytes, hashes, page counts, and gold-span page placement):

```powershell
python eval/v0.1/verify.py --download
```

Both commands exit non-zero on any mismatch. CI runs the offline check on every
push. A Phase 5 evaluation run must use the live check before trusting a score.

## Why the lock is stricter than a normal fixture

- A filename is not an identity. Any re-encoded, re-paginated, or substituted
  PDF is a different corpus and must fail loudly rather than silently change a
  retrieval score.
- Page numbers are part of the citation contract. A PDF with the same text but
  different pagination would make every stored citation wrong.
- Gold spans are matched after collapsing whitespace, because PDF extraction
  varies in line breaks and spacing between library versions.
- The manifest makes an accidental edit to a question or the schema visible
  immediately, even when nobody runs the download check.

The freeze was recorded as **D-029** in
[`docs/phase-1/07-decision-log.md`](../../docs/phase-1/07-decision-log.md).
