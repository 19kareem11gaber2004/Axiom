# Axiom — Arabic Scope (Phase 1, Step 7)

**Decision [v0.1]: Arabic retrieval is OUT of scope; Arabic OCR is IN scope (Phase 7).**

v0.1 retrieval and evaluation are English-only. consequences: the Phase 5 embedding shortlist is English-optimized models only (multilingual candidates excluded); Arabic enters in Phase 7 as OCR/ingestion with its own eval questions, and Arabic *retrieval* only if Phase 6 measurements justify a multilingual embedding switch plus full re-index.

Revisit trigger: a frozen Arabic corpus with ≥20 verified questions and a measured Recall@5 gap on a multilingual candidate versus the v0.1 embedder.

Logged: `07-decision-log.md` (D-009).
