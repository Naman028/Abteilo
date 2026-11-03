# Enterprise Intelligence Layer — Design (Blocks B, C and D)

**Status (2026-10-20):**
- **Block B (hybrid retrieval):** implemented and measured, developer-tested. See `EVALUATION.md` and the Block B section of `ACCEPTANCE.md`.
- **Block C (knowledge graph):**
  - implemented (migrations `0005`, `0006`) and accepted for development;
  - verified in Docker (PostgreSQL 17) and WSL (PostgreSQL 18);
  - see the Block C section of `ACCEPTANCE.md` and GRAPH-01..11 in `ENGINEERING_CONTRACT.md`.
- **Block D (combined answers):** implemented on the developer machine, with acceptance pending review. See `BLOCK_D_DESIGN.md` (design and as-built differences) and `ACCEPTANCE.md`.

## Goal

Answer cross-department questions such as *"Why is A17 blocked, what does it
do to the budget, and what should we discuss tomorrow?"* by combining four
sources under the existing authorisation and verification rules:

1. hybrid document retrieval;
2. a provenance-aware relationship graph;
3. live business records;
4. the evidence verifier.

## Block B — Hybrid retrieval

| Part | Choice | Notes |
|---|---|---|
| Vectors | `pgvector` column on `core.document_chunks` (`embedding vector(n)`, `embedding_model`, `embedded_at`) | Same rows as full-text search, so the same RLS and visibility rules apply (RAG-01) |
| Embedding model | One local multilingual model served by Ollama (candidates: `bge-m3`, `nomic-embed-text`) | Pick by measured recall on an English/German question set |
| Candidates | Top-k full-text and top-k vector, both filtered by the visibility predicate **inside SQL** | Never filter after the model has seen results |
| Fusion | Reciprocal Rank Fusion (k = 60) | Deterministic, no tuning data needed |
| Reranker | Optional small local cross-encoder; behind a flag until it beats RRF on the evaluation set | Bounded to the top 20 candidates |
| Indexing | Background job keyed by `document_versions.id`; status per version (`pending`, `indexed`, `failed`) visible in Data & retention | RAG-02, RAG-04 |
| Revocation | Visibility is checked against `documents` and role assignments at query time, so a pending re-index never widens access | RAG-03 |
| Filtered ANN | Start with exact search (company scale is small); add HNSW with iterative scan only when measured latency requires it | pgvector filtered-recall caveat |

## Block C — Relationship graph (PostgreSQL first, implemented)

The original sketch (`graph_nodes` / `graph_edges` with an edge `status`) was built as follows:

```text
core.graph_entities(company_id, id, kind, ref_id, label, status, vis_scope, vis_department_id, vis_user_id,
                    active, person_user_id -> memberships, quarantine_reason, quarantined_at)
core.graph_relationships(company_id, id, src_id, dst_id, kind, authority [record|document_mention|reviewed],
                         document_id, valid_from, valid_to, ended_reason)
core.graph_evidence(relationship_id, source_type, source_id, document_version_id, quote)
core.graph_candidates(...)   -- model suggestions; never traversed until a reviewer accepts (GRAPH-06)
```

Where the build differs from the sketch:
- **"Provisional" edges.** They are not edges with a status. They live in `graph_candidates` and become a `reviewed` relationship only through a reviewer's decision.
- **Projection.** It is an idempotent rebuild run by the worker (GRAPH-04), not an outbox.
- **Tenant safety.** Composite `(company_id, id)` keys plus the person-membership key from `0006` (GRAPH-11).

The bullets below are the original design intent, which still holds:

- **Authoritative edges come from records**, not from models: project→task,
  task→assignee, project→purchase_request, request→approval,
  department→project. They are rebuilt by the outbox/projection job whenever
  their source rows change (GRAPH-04).
- **Edges extracted from documents** start as `provisional` (GRAPH-03). They
  become `authoritative` only after an authorised reviewer accepts them in the
  owner console. Every edge stores its source reference.
- **Traversal** is a recursive CTE with a hop limit (≤ 3) and a node budget.
  Every node *and* edge is checked with the same visibility predicate as
  documents and records (GRAPH-01/02). A path containing a hidden node is cut
  at that node; the hidden node is never named in the answer.
- **Storage boundary:** the graph is reached through one `GraphService`
  interface, so it can move to Neo4j later if measured traversal needs
  justify it. The orchestrator does not change.

## Block D — Combined answers

Superseded by the full design in `BLOCK_D_DESIGN.md`, which refines this sketch:
- information needs;
- the sufficiency gate;
- the graph to live-record re-read;
- causal, freshness and audit rules.

Original sketch:

- New tools: `graph.neighbours(entity)` and `graph.path(a, b)`, plus hybrid
  `documents.search`.
- Each tool returns evidence with authority (`record`, `aggregate`,
  `relationship`, `document`) and an as-of time.
- The verifier gains a `relationship` authority. A causal claim can be
  **verified** only when an authoritative edge links cause and effect, for
  example `A17 blocked_by task T, T requires component C, C requested_in
  PR-12, PR-12 status=submitted` (EVD-01). Without such an edge it stays
  `cited` or `unsupported`, as today.

## Evaluation (each step must earn its place)

The Block D evaluation plan is in `BLOCK_D_DESIGN.md` §6. The plan below was the original intent; Block B measured its first rows (`EVALUATION.md`).

A fixed question set of about 60 questions across the four departments and the
owner workspace, each with gold evidence and per-role expected visibility.
Configurations compared:

| Configuration | Measured |
|---|---|
| Keyword only (today) | Recall@5 and @10, citation correctness, latency |
| + vector | same |
| + RRF / reranker | same |
| + graph | multi-hop correctness, causal-claim verified rate |

Release gates:
- zero unauthorised retrievals in the security set;
- no regression in claim correctness;
- p95 retrieval latency under 1.5 s on the demo data.

## Invariants to add to the contract

RAG-01..04, GRAPH-01..04, EVD-04 (citation re-authorisation, already
implemented), OPS-01 (a broken index or graph falls back to keyword search,
never to wider access) and OPS-02 (bounded retrieval time and retries).
