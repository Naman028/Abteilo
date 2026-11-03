# Four-Department MVP — Acceptance Report

**Scope:** Engineering, Finance, Human Resources, IT & Security, and the owner
console. No new departments, retrieval features or models were added in this
block.

**Status: acceptance criteria met on the developer machine. Independent
re-run pending.** This is a functional MVP, not a production-ready
release (see section 6).

Recorded 2026-10-19. Environment:
- Windows 11 with WSL Ubuntu, PostgreSQL 18.6 (tests);
- Docker Desktop 29.1.3, PostgreSQL 17.6 (deployment check);
- Python 3.13.2, Node 22.14.0, Ollama 0.31.1 with qwen2.5:7b.

## 1. How to reproduce

```bash
cd backend
python -m pytest            # needs ABTEILO_TEST_DATABASE_URL and ABTEILO_TEST_MIGRATION_DATABASE_URL
```

Result: **45 passed** (last full run: `45 passed in 32.04s`). Each test
migrates a fresh schema state and seeds its own company. Tests connect as the
real runtime role (no `BYPASSRLS`, owns nothing).

## 2. Requirements, invariants, implementation and tests

| ID | Requirement | Implemented in | Automated tests |
|---|---|---|---|
| OWN-01 | An active company keeps at least one active owner. Every owner-removing operation locks the company row, changes, and re-checks. | `company.lock_company`, `company.ensure_active_owner`, `admin.set_member_status`, `modules/it.revoke_assignment` | `test_acceptance_security::test_concurrent_owner_removals_keep_one_active_owner` (two real transactions, see 3.1); `::test_owner_cannot_be_removed_through_any_path` |
| SEC-06 | Owner and security-admin roles are granted only through an owner-approved access request; no direct path | `modules/it.api_decide_access`, `admin.api_assign_department_role`, `admin.api_create_member`, migration `0002` | `test_acceptance_security::test_no_endpoint_grants_elevated_roles_directly`; `test_access_and_workflows::test_elevated_roles_need_the_owners_approval` |
| SEC-03 / matrix | Every `/api/admin` endpoint is enforced against its intended permission, including non-owner holders (security admins hold `audit.read` and may revoke ordinary roles) | `admin.py`, `overview.py` | `test_acceptance_security::test_admin_permission_matrix`. It fails if any admin route is missing from the matrix. |
| SEC-07 | Password reset and account disable invalidate sessions on every protected route | `security.revoke_user_sessions`, `admin.api_reset_password`, `admin.set_member_status` | `test_acceptance_security::test_old_sessions_are_rejected_by_every_protected_route[password_reset / account_disabled]` (more than 60 route-method pairs each) |
| SEC-08 | Pooled connections never carry tenant or user context, including after a rollback | `db.transaction` (transaction-local `set_config`) | `test_acceptance_security::test_pooled_connections_do_not_leak_context` |
| AUD-CSV | Audit export is permission-checked and neutralises formula cells (`= + - @ \t \r \n`). Quotes, commas and semicolons round-trip exactly. | `overview.api_audit_csv` | `test_acceptance_security::test_audit_csv_is_authorised_and_formula_safe` |
| AI-04 | Summary and claims are verified. "Verified" requires a structured fact matched against a live record. | `ai/evidence.py`, `ai/orchestrator.py` | `test_assistant_and_privacy::test_claim_verification_rules`, `::test_summary_is_verified_like_a_claim` |
| EVD-01 | Causal claims are never verified by word matching | `ai/evidence._causal_split`, `_causal_link_in_source` | `::test_causal_stale_and_conflicting_evidence` (invented cause → unsupported; cause stated in a source → cited) |
| EVD-02 | Live records override older documents for current state | `ai/evidence._status_conflicts` | same test: `outdated`, `document_not_authoritative_for_current_state` |
| EVD-03 | Conflicts are surfaced; evidence shows authority and as-of time | `ai/evidence`, `ai/tools.Evidence.as_of`, `Assistant.tsx` | same test: `conflicting`, `document_disagrees_record_preferred` |
| EVD-04 | Citation previews re-check permission; stored chats keep no evidence content | `modules/documents.open_document`, `assistant._strip_payload` | `::test_citation_previews_recheck_permission` |
| AI-07 | Assistant tools are offered and run only while their department module is on | `ai/tools._TOOL_MODULES`, `orchestrator.available_tools` | `test_acceptance_management::test_switching_a_module_off_removes_screens_and_assistant_tools` |
| MGT | Owner overview arithmetic, routing, safe model failure, account enable/disable | `overview.py`, `ai/gateway.py`, `admin.py` | `test_acceptance_management::*` (4 tests) |
| Earlier invariants | DATA-01/02, SEC-01..05, AI-01..06, retention, export, workflows | see `ENGINEERING_CONTRACT.md` | `test_database_invariants` (14), `test_access_and_workflows` (10), `test_assistant_and_privacy` (rest) |

## 3. Evidence

### 3.1 Concurrency test (OWN-01)
Transaction 1 (owner Anna) revokes co-owner Lena's owner role and holds its
transaction open. Transaction 2 (Lena, over HTTP) tries to disable Anna. The
test confirms through `pg_stat_activity` that transaction 2 is waiting on a
row lock; this is a condition wait, not a fixed sleep. Transaction 1 then
commits, and transaction 2 re-checks and returns **409**. Exactly one active
owner remains.

**Mutation check:** with `lock_company` turned into a no-op, the same test
**fails** (transaction 2 never waits), so the test really depends on the lock.

### 3.2 Browser verification (built frontend, local Qwen 2.5 7B)
- **Owner console:** Overview showed A17 blocked work with its blocked task,
  pending counts (1 purchase at 2,400 €, 1 leave request, 1 role change), the
  department table and AI health. People & roles opened the per-person panel
  (details, password reset, disable, role assign/revoke). The AI briefing
  produced a verified claim about A17 citing the company overview.
- **Assistant, question "Why is project A17 blocked?":**
  - First run, before the fix: two narrative sentences from the thermal report
    were labelled ✓ verified, one because it contained "A17" and one because it
    contained "4". **This was a defect.**
  - The verifier was fixed so that "verified" requires a structured fact
    matched against a live record. A regression test with these exact
    sentences was added.
  - Re-run after the fix: both sentences show **≈ cited**, with the reasons
    "wording not machine-checked" and "only a document says this", and the
    answer status is "Partly verified". The causal summary is flagged ⚠
    (unsupported). Evidence chips show "live record" and "document" with dates.

### 3.3 Database verification
All tests run as the runtime role against PostgreSQL 18.6:
- `DELETE` on business tables is denied;
- audit events are append-only;
- with no tenant context, every query returns zero rows;
- composite foreign keys stop a company role being assigned to a department.

## 4. Docker deployment

Commands: `docker compose --env-file deploy/.env.docker -p abteilo ...` on port
8080. Before starting, there were no existing Abteilo containers or volumes, so
nothing was overwritten.

| Check | Result |
|---|---|
| Clean build | Pass. One shared image `abteilo-app:local` (frontend build + Python runtime), non-root user `abteilo`. |
| Image contents | Pass. No `.env`, `.venv` or `node_modules` inside (a `.dockerignore` was added this block). |
| Migrations | Pass. The `migrate` service ran `alembic upgrade head` as the owner role and exited 0. Re-running it after recreation also exited 0. |
| Health | Pass. `/healthz` 200, `/readyz` 200 (database reachable), API container `healthy`. |
| Production hardening | Pass. OpenAPI and docs return 404 when `ABTEILO_ENVIRONMENT=production`. |
| Seed and login | Pass. The seed created 7 accounts; owner login 200; overview from the container: 7 members, 4 departments, A17 blocked, Ollama reachable from the container. |
| Worker | Pass. It ran a retention pass (`retention_run`); its health check is disabled because it serves no HTTP. |
| Restart | Pass. Counts were identical before and after (`7 users | 3 projects | 16 audit events | retention 30`), and the session persisted. |
| Down and up (volume kept) | Pass. Same counts, volume `abteilo_pgdata` retained, login 200. |
| Volume removal | Not performed, deliberately (it would delete data). |

The stack was left stopped with its volume kept.

## 5. Defects found and fixed during this block

| Defect | Severity | Fix |
|---|---|---|
| Two concurrent account disables could remove every owner | P0 | Company-row lock protocol (OWN-01) |
| Narrative from a document was marked verified because it contained an identifier or a number | P0 | "Verified" requires a structured fact from a live record; regression test added |
| Causal explanations could pass as verified | P0 | EVD-01 |
| Switched-off modules still exposed their assistant tools | P1 | AI-07 |
| `[E1]` citation markers were read as project codes | P1 | Markers stripped before identifier checks |
| Docker build context would have included `.env`, `.venv` and `node_modules` | P1 | `.dockerignore` |
| Worker reported unhealthy (it inherited the HTTP health check); image was built three times | P2 | Compose: one shared image; worker health check disabled |
| CSV export did not neutralise tab, carriage-return or newline formula starts | P2 | Prefix list widened |

## 6. Remaining limitations and risks

- **Not production-ready.** Not yet demonstrated:
  - HTTPS termination;
  - managed secrets;
  - automated encrypted backups with a tested restore;
  - monitoring and alerting;
  - load and performance testing;
  - a security review by someone other than the developer.
- **Independent re-run pending.** These results come from the developer machine.
- **Fact-checking limits.** Verification checks numbers, statuses, identifiers,
  causes, freshness and conflicts deterministically. It cannot prove the
  meaning of narrative text, which therefore stays "cited". Conflict detection
  covers status disagreements about named subjects (project codes, quoted
  names), not every kind of contradiction.
- **Search.** Retrieval is keyword-only (PostgreSQL full-text). Vector search
  and the knowledge graph are designed in `INTELLIGENCE_DESIGN.md` but not
  implemented.
- **Model speed.** The local 7B model takes about 30–60 seconds per answer on
  this machine. Quality and latency depend on hardware.
- **Sign-in.** Password login with server-side sessions. No MFA or OIDC yet.
- **No company onboarding UI.** Companies are created by the seed or by
  `company.create_company`.

---

# Block B: Hybrid Retrieval — Acceptance

**Scope:**
- pgvector embeddings;
- keyword + vector search with Reciprocal Rank Fusion;
- permission-aware retrieval;
- document versioning with index status;
- the evaluation harness.

Not built in this block: knowledge graph, reranker, new departments.

**Status: acceptance criteria met on the developer machine. Independent re-run pending. Not production-ready.**

## Requirements, implementation and tests

| ID | Requirement | Implemented in | Automated test (`tests/test_hybrid_retrieval.py`) |
|---|---|---|---|
| RAG-01 | Keyword and vector search filter by visibility inside SQL | `modules/documents._keyword_candidates`, `_vector_candidates` | `test_semantic_match_finds_paraphrases_but_never_restricted_chunks` (vector-identical query from an unauthorised user) |
| RAG-02 | Chunks and embeddings belong to one immutable version; only the current version is searched | migration `0003` (column-level grants), `documents.add_version` | `test_only_the_current_version_is_searchable_and_versions_are_immutable` (also proves `UPDATE` of chunk text is denied) |
| RAG-03 | Revocation is immediate, without re-indexing | visibility is evaluated at query time | `test_revocation_takes_effect_without_reindexing` |
| RAG-04 | Index status per version; bounded retries; owner can re-queue | `indexer.py`, `worker.py`, `admin.api_retry_index` | `test_index_failures_are_bounded_visible_and_retryable` |
| OPS-01 | An embedding outage degrades to keyword-only and never widens access | `documents.hybrid_search` | `test_embedding_outage_degrades_to_keyword_without_widening_access` |
| Fusion | Reciprocal Rank Fusion ordering | `documents.fuse` | `test_reciprocal_rank_fusion` |
| Matrix | New admin route covered by the permission matrix | `admin.api_retry_index` | `test_acceptance_security::test_admin_permission_matrix` |

The tests use a deterministic synonym-aware embedder; they never call a real model. Full suite: **51 passed**.

## Measured retrieval quality

See `EVALUATION.md`. Summary with bge-m3 in hybrid mode:
- Recall@5 1.00 and MRR@10 0.961 on 38 development questions;
- 1.00 / 1.00 on 8 held-out questions;
- **0 unauthorised results and 0 security-set violations**, across all models, modes and thresholds.

## Deployment verification

**WSL Postgres 18 (development and tests):**
- `postgresql-18-pgvector` 0.8.7 installed;
- extension created by the database superuser in `abteilo` and `abteilo_test`;
- migration 0003 applied, rolled back and re-applied.

**Docker:**
- The database image switched to `pgvector/pgvector:0.8.7-pg17-bookworm`. Same major version, so the existing `abteilo_pgdata` volume was reused; no data was removed.
- The migration ran to revision `0003` and exited 0.
- The worker indexed the 7 existing documents with bge-m3: `{"event":"index_run","indexed":7,"failed":0}`.
- Over HTTP, a German question ("Wie viele Urlaubstage habe ich?") returned the English Leave Policy as a semantic match, in hybrid mode with no degradation.
- The stack was left running.

**CI:** the workflow now uses the same pgvector image.

## Defects found and fixed during Block B

| Defect | Fix |
|---|---|
| Chunks embedded without their title missed title-only topics (for example "Salary Bands") | Each chunk is embedded as `title + content` |
| Keyword OR-matching on question words added noise to fusion (hybrid MRR 0.817) | English/German stopwords, plus AND-matching inside hybrid (MRR 0.961). Recall-oriented OR is kept for keyword-only mode and the fallback. |

## Remaining limitations

- The evaluation corpus is small and synthetic. The held-out set has only 8 questions.
- No reranker. Exact (non-indexed) vector search; HNSW is the next step if latency grows.
- The assistant's tool path embeds the query inside its transaction (about 0.4 s of held connection).
- Knowledge graph (Block C) and combined retrieval (Block D) are not built.

---

# Block C: Knowledge Graph — Acceptance

**Scope:**
- PostgreSQL knowledge graph projected from business records (migrations `0005`, `0006`);
- provenance per edge;
- reviewer-gated suggestions from documents;
- bounded, permission-aware traversal;
- graph evaluation.

Not built in this block: combined retrieval (Block D), Neo4j, new models, new departments.

**Status: accepted as a developer-tested milestone (reviewer decision, 2026-10-20). Not production-accepted.** The reviewer did not re-run the repository or verify it in Docker. The defect is recorded as a resolved development-environment isolation defect with incomplete historical access visibility, not as "no disclosure possible".

Recorded 2026-10-20. Environment:
- WSL Ubuntu, PostgreSQL 18 (development and tests);
- Python 3.13, Node 22.

## Requirements, implementation and tests

| ID | Requirement | Implemented in | Automated test (`tests/test_knowledge_graph.py`) |
|---|---|---|---|
| GRAPH-01/02/08 | Visibility per entity from live grants; hidden entities cut paths; the same `NOT_FOUND` for missing and hidden | `graph._entity_visible`, `_edge_visible` | `test_engineer_finds_dependencies_but_no_finance_or_hr_facts` |
| GRAPH-03 | No edge without evidence | `0005` deferred constraint trigger | `test_projection_is_idempotent_evidence_backed_and_append_only` |
| GRAPH-04 | Idempotent projection | `graph_projection` | same test, and `test_projection_stays_inside_the_company` (two tenants, repeated runs) |
| GRAPH-05 | Edges ended, never deleted or reopened | `0005` trigger and grants | `test_changed_and_archived_records_end_outdated_edges` |
| GRAPH-06 | Model suggestions are not edges until reviewed | `graph_candidates`, `graph.decide_candidate` | `test_suggestions_never_become_edges_without_review` |
| GRAPH-07 | Bounded traversal; hubs do not connect unrelated work | `graph` limits, 2 s timeout | `test_traversal_is_bounded_and_hubs_do_not_connect_unrelated_work` |
| GRAPH-09 | Evaluation with zero restricted leaks | `eval/graph.py` | `test_graph_evaluation_cases` |
| GRAPH-10 | A failed run rolls back as a whole | `graph_projection.project_company` | `test_projection_failure_rolls_back_and_never_widens_access` |
| GRAPH-11 | A company's graph contains only that company's people and records | scoped person query; `0006` membership key, quarantine and grants | `test_projection_stays_inside_the_company`, `test_database_refuses_foreign_people_whatever_the_projection_does`, `test_migration_quarantines_existing_foreign_people` |

## Defect: cross-tenant person projection (P0)

- **Symptom.** BINA GmbH's graph showed 27 people instead of 7.
- **Cause.** The person projection read `core.memberships` with no company filter. `memberships` and `users` are the only projection sources without RLS, so tenant RLS on the graph tables could not catch it: the worker wrote other companies' members into each graph, labelled as that company's rows.
- **Window (development database only).** From 2026-10-20 01:57:15 until migration `0006` on 2026-10-20 at about 11:18.
- **Affected data.** All three companies in the development database:

  | Company | People in graph | Foreign people (active, visible company-wide) |
  |---|---|---|
  | BINA | 27 | 20 |
  | Retrieval-evaluation company 1 | 27 | 17 |
  | Retrieval-evaluation company 2 | 27 | 17 |

  Each foreign row held another company's user id, display name and membership status. All of it is fictional demo data. The Docker database is at revision `0003`, has no graph tables and was never affected.
- **Remediation.**
  1. The person query is scoped to `core.current_company_id()`.
  2. Migration `0006` adds a database guarantee. A non-quarantined person entity references `core.memberships(company_id, user_id)` through the generated column `person_user_id`; the key is added `NOT VALID`, existing rows are remediated, then the key is validated.
  3. The 54 contaminated rows were quarantined, not deleted:
     - set inactive, with the label replaced;
     - reason and time recorded;
     - one audit event per company with the counts;
     - current edges to them ended, open suggestions on them superseded (there were none).
  4. A quarantined row can never be active again (check constraint). The runtime role has no privilege to set or clear the quarantine (column-level `INSERT`/`UPDATE` grants).
  5. Uniqueness now covers non-quarantined rows only, so a person who later really joins the company gets a new, validated entity.
- **Backup and restore.** Before the migration, a `pg_dump` of the development database was restored into a scratch database and compared by row counts (identical), then the scratch database was dropped. The dump stays outside the repository, in the WSL postgres home (`backups/abteilo_pre_0006_20261020.dump`). It still contains the unredacted rows.

### Exposure investigation (read-only, before remediation)

- **Stored in the wrong tenant: confirmed.** Counts as in the table above.
- **Aggregate count disclosed: confirmed.** The owner console (`/api/admin/graph`) showed 27 people to BINA's owner; this is how the defect was found.
- **Names disclosed: not confirmed, and cannot be ruled out.**
  - The foreign entities had no relationships, suggestions or evidence, so no neighbourhood or path query could reach them.
  - They were reachable only by direct lookup (`/api/graph/person/{user_id}/neighbourhood`), which needs the other company's user UUID.
  - Graph reads are not audited and request logs were not kept, so whether such a lookup happened cannot be determined.
  - Activity during the window: two BINA logins (01:58) and one candidate decision (02:01). The other two companies had none.
- **AI context: not exposed.**
  - The extraction prompt lists only project, task and purchase-request entities.
  - There were no assistant runs and no chat messages during the window.
  - The graph is not yet used by the assistant (Block D).
- **Reports, evidence, audit: no references.**
  - No graph evidence, chat payload or audit event in an affected company refers to the foreign users.
  - The 64 audit references to those user ids are their own companies' `member.added` and `auth.login` events.

### Fail-then-pass check

| Code | Result of `test_projection_stays_inside_the_company` |
|---|---|
| Pre-fix unscoped query restored | Fails: `CardinalityViolation` (the shared user appears twice) |
| Unscoped query, de-duplicated (`DISTINCT ON`) | Fails: `ForeignKeyViolation` on `graph_entities_person_membership_fk` |
| Secure query restored | Passes |

## Evidence

- **Full suite: 63 passed** against PostgreSQL 18. The session fixture migrates `base -> head` and back, which exercises the `0006` upgrade and downgrade. `test_migration_quarantines_existing_foreign_people` covers three more steps:
  - downgrades to `0005`;
  - plants the defect's footprint (a foreign person and an edge to them);
  - upgrades, and proves that a downgrade is refused while quarantined rows exist.
- **Graph evaluation** (`python -m eval.graph`, development database after `0006`): correct 6/6, false paths 4/4, restricted 7/7, **0 restricted leaks**. Two consecutive projection runs over all companies changed nothing.
- **Frontend build:** `npm run build` passes.
- **Browser (built frontend, disposable test database seeded with BINA and a second company, Nordwerk; Jonas is a member of both):**
  - Jonas's Engineering projects list A17, B24 and C08 only.
  - A17's connections at depth 3 show 14 edges, and the only people are Lena and Jonas. Nothing from Nordwerk appears.
  - The task assignee list offers BINA's 8 members only.
  - Nordwerk's project and Nordwerk's own employee return `404 NOT_FOUND` from Jonas's BINA session.
  - The BINA owner console reports 7 person records, and still 7 after "Update now".

## Other findings carried forward

- **AI relationship extraction (P1).** The development log reports that the local Qwen 2.5 7B proposed two relationships and both were wrong. Two samples do not measure accuracy, but they rule out automatic promotion. Suggestions stay provisional, carry their exact source sentence and require an authorised reviewer (GRAPH-06). Not re-measured in this session.
- **Unanswerable questions (P1, Block D).** The development log reports that 5 of 10 unanswerable questions were rejected. Similarity is not evidence that an answer exists; Block D must judge answer sufficiency. Not re-measured in this session.

## Remaining limitations and risks

- Graph reads are not audited, so a past disclosure through direct lookups cannot be ruled out. Adding read auditing needs a decision on volume and on GDPR purpose limitation.
- The database key covers person entities, the only kind whose sources lack RLS. Other kinds rely on forced RLS on their source tables and the `(company_id, id)` keys of edges.
- Quarantined rows keep other companies' user ids (UUIDs, no names) in the affected tenant for traceability.
- The pre-migration dump contains the unredacted names. Delete it once this review is closed.
- `0006` cannot be downgraded while quarantined rows exist. This is intentional.
- The Docker stack still runs the Block B image (revision `0003`). Block C has not been deployed there.
- The membership key proves that a membership exists, not that it is active.
  - **Covered.** A suspended or disabled member is rejected on every request: each session resolves against `users.status` and `memberships.status`. This includes all graph routes, and `test_acceptance_security::test_old_sessions_are_rejected_by_every_protected_route` covers it.
  - **Not covered.** The suspended person's own graph node stays visible to colleagues until the worker's next projection pass, which marks it inactive.

## Open items after acceptance

These are tracked here; Block C is not reopened for them.

| Priority | Item | Required before |
|---|---|---|
| P1 | Proportionate audit of graph reads: user, company, requested resource, time, authorisation result and correlation id, never response content | Real-company deployment |
| P1 | A test that suspending or revoking a member hides their graph node immediately (today it waits for the next projection run) | Production release |
| Done | Apply migrations `0004`-`0006` in an isolated Docker environment and re-run acceptance: see "Docker verification (PostgreSQL 17)" below | Deployment acceptance |
| P1 | The Docker migration role is a superuser (finding D1 below) | Production release |
| P2 | The image tag `abteilo-app:local` is shared between Compose projects (finding D2 below) | Next deployment change |
| P1 | Test the assistant's live graph and evidence interactions with a running model | Block D completion |
| P1 | Retention decision for the pre-migration dump: preserve the investigation evidence, confirm current data is recoverable, then dispose of it with the owner's approval. Never delete it automatically. | Closing the incident record |
| P1 | Answer sufficiency: only 5 of 10 unanswerable questions were rejected | Block D completion |
| P1 | Relationship extraction quality: both sampled model suggestions were wrong | Any autonomous extraction |

## Docker verification (PostgreSQL 17)

Recorded 2026-10-20 on Docker Desktop. Image `pgvector/pgvector:0.8.7-pg17-bookworm` (PostgreSQL 17.11) with the Block C application image. These results are separate from the PostgreSQL 18 (WSL) results above.

### Isolation

- Two Compose projects, `abteilo-verify` (upgrade) and `abteilo-verify-fresh` (fresh install), use an override file and test-only credentials.
- `docker compose config` resolved the following:

  | Resource | Upgrade (`abteilo-verify`) | Fresh install (`abteilo-verify-fresh`) |
  |---|---|---|
  | Volume | `abteilo-verify_pgdata` | `abteilo-verify-fresh_pgdata` |
  | Network | `abteilo-verify_default` | `abteilo-verify-fresh_default` |
  | Ports | 15432 (db) / 18080 (api) | 15433 (db) / 18081 (api) |

  The image tag was `abteilo-app:verify-blockc`. There were no `container_name`s and no external volumes or networks. The only shared resource is the read-only `deploy/initdb` mount.
- Before and after the run, the existing stack had:
  - the same container ids and the same `abteilo-app:local` image id (`sha256:9f4c94b4…`);
  - the same `abteilo_pgdata` volume and network;
  - identical business row counts (revision `0003`, 1 company, 7 users).

  It stayed running throughout (`/readyz` ready).
- Both verification stacks were stopped afterwards; their volumes are kept for inspection.

### Scenario A: fresh install (`base -> head`)

- `migrate` exited 0 at revision `0006`, with 35 tables.
- Structure:
  - all 28 RLS tables in `core` have forced RLS, the 6 graph tables included;
  - the 5 `0006` constraints are validated;
  - the runtime role holds no `INSERT`/`UPDATE` on quarantine columns.
- The standard demo seed (`python -m abteilo.seed`) worked.
- Graph evaluation: correct 6/6, false paths 4/4, restricted 7/7, **0 restricted leaks**. Two rebuilds changed nothing. The people in each graph equal the company's active members.

### Scenario B: upgrade of an existing installation (`0003 -> 0006`)

- **Pre-upgrade dataset.** It was written by the Block B image itself (`abteilo-app:local`, migrations up to `0003`):
  - the BINA demo seed;
  - a second company (Nordwerk) with its own project, tasks and document;
  - a user who is a member of both companies;
  - a suspended membership;
  - a newer document version and an archived document;
  - a chat.

  In total: 29 tables, 309 rows.
- **Migration only.** The same dataset in a second database, upgraded with no worker running: **29 of 29 pre-existing tables unchanged** (row count and MD5 over all rows, using the `0003` column set). The only additions are the 6 graph tables.
- **Running deployment.** After `migrate`, API and worker: 27 of 29 tables unchanged. The two changes come from the worker, not the migration:
  - `core.ai_runs` gained 5 rows from relationship extraction attempts;
  - `core.document_versions` changed index-retry fields.

  No model runs in the isolated environment, so extraction and indexing failed with safe error codes.
- **Structure.**
  - `0006`, with the same RLS, constraint and grant results as scenario A;
  - the runtime role is `NOSUPERUSER NOBYPASSRLS`;
  - 0 foreign people and 0 cross-company edges;
  - the pre-upgrade suspended member's node is inactive.
- Two rebuilds after the upgrade changed nothing.
- **Runtime checks over HTTP: 21/21 passed.**
  - Cross-company project and person lookups return 404 in both directions; hidden and missing give the same `NOT_FOUND` code.
  - The shared user's BINA graph contains no Nordwerk work.
  - Owner people counts: BINA 6 (7 members, 1 suspended), Nordwerk 3.
  - The pre-upgrade suspended member cannot sign in.
  - Citations: a visible document opens. The confidential HR document, the archived document and the other company's document are refused.
  - Graph failures: depth above the bound and an unknown entity kind return 422; a cross-company path is not resolved.
  - A live suspension is rejected on graph routes at once. The suspended person's node stayed visible to a colleague (200) until the next projection run (then 404): the open P1 is confirmed.
- **Graph evaluation on the upgraded dataset: 16/17, 0 restricted leaks.** False-path case F3 targets the Supplier Note, which this dataset archived on purpose. The target is correctly hidden from Jonas, so the case cannot be evaluated on this data; on the standard seed (scenario A) it passes.

### Integration suite on PostgreSQL 17

- **63 passed** against an isolated database (`abteilo_test`) in the same container.
- The schema owner there is a non-superuser role (`abteilo_vtest_owner`), as on WSL, so FORCE RLS applies to the owner too, which several tests rely on.
- The session fixture migrates `base -> head` and `head -> base`. The graph tests also run `head -> 0005 -> head`.

### Findings

- **D1 (P1): in Docker the migration role is a superuser.** `abteilo_owner` is `POSTGRES_USER`, so it is a superuser and bypasses RLS. Migrations and the RLS-dependent behaviour still pass, and the application runs as the restricted `abteilo_app`. Least privilege still asks for a non-superuser schema owner, with the superuser only creating the extension and roles in `deploy/initdb`.
- **D2 (P2): the image tag is shared between projects.** `docker-compose.yml` pins `image: abteilo-app:local`, so building the file under any project name retags the image that the running development stack uses. The verification avoided this with its own tag. The tag should come from a variable (for example `ABTEILO_IMAGE`).
- **Upgrade of the long-running Docker stack itself:** not done. It is still at `0003` by design. Upgrading it is a separate decision.

### Reproduction

Run Compose with the project name, the override file and the env file:

```bash
docker compose -p abteilo-verify -f docker-compose.yml -f compose.verify.yml --env-file verify.env up -d --no-build
```

- **Override (`compose.verify.yml`):** publishes db 15432, api 18080 and the `abteilo-app:verify-blockc` image.
- **Build:** do it first, with the same flags and `build migrate`.
- **Pre-upgrade migration and seed:** run them with `docker run --network abteilo-verify_default abteilo-app:local …` before the build.
- **Fingerprints:** row count and MD5 per table, taken over the `0003` column set before and after.

---

# Step 1: local model integration smoke test (before Block D)

Recorded 2026-10-20.

**Environment:**
- The current code (Block C plus the fix below), served over HTTP.
- PostgreSQL 18 (WSL), on the disposable test database.
- **Ollama 0.31.1:**
  - `qwen2.5:7b` for every department and the owner (route `general`);
  - `bge-m3` for document embeddings.
- **Machine:** Intel Core Ultra 9 185H (16 cores), 15.5 GB RAM, NVIDIA RTX 4060 Laptop GPU (8 GB).
- **Data:** BINA (demo seed, documents indexed with real bge-m3 embeddings, graph built) plus a second company, Nordwerk, with a user who belongs to both.
- No paid or external model was called.

## Results

| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | Ollama reachable; `qwen2.5:7b` and `bge-m3` installed and loading | PASS | Qwen fully on the GPU: 5.47 GB, 16k context |
| 2 | Gateway makes a real Qwen request | PASS | Admin model test returns `ok`. 8.5 s with the cold model load; about 1 s warm. |
| 3 | Planning JSON validates against the backend schema | PASS | 42 plan calls, 0 invalid outputs (`core.ai_runs`) |
| 4 | Permitted tool executed through the backend | PASS | `tools_used` in every department case below |
| 5a | Real Qwen asked to use HR, Finance and IT tools from Engineering | PASS | Only `documents.search` ran |
| 5b | Injected plan naming `hr.pending_leave` and `finance.list_purchase_requests` | PASS | Only `engineering.get_project` executed. The plan stage was replaced, because the schema already stops Qwen from naming tools outside the permitted set; this proves the server-side re-check. |
| 6 | Engineering: A17 status and blocked tasks (Jonas) | PASS | `engineering.get_project`; 2 claims verified |
| 7 | Finance: Engineering budget and pending purchases (Sara) | PASS | `finance.budget_summary`, `finance.list_purchase_requests`; 2 verified |
| 8 | HR: pending leave (Mila) | PASS | `hr.pending_leave`; 2 verified, 1 unsupported (shown as such) |
| 8b | Leave policy through hybrid search (Jonas) | PASS | `documents.search`; 1 cited |
| 9 | IT & Security: open access requests (Tim) | PASS after fix | `it.list_access_requests`; 1 verified, 1 unsupported |
| 10 | Owner: what needs attention | PASS | `company.overview`, `company.department_activity`; verified |
| 11a | Engineer asks for salary bands | PASS | Nothing retrieved; `insufficient_evidence`; no salary figure anywhere |
| 11b | Engineer asks about the other company's project and employee | PASS | Evidence holds BINA projects only. The summary repeats the user's own words and says the status cannot be determined. |
| 11c | Finance analyst asks for HR leave data | PASS | No HR tool ran; no other person's leave shown |
| 11d/e | Employee calls the owner assistant, or another department's assistant | PASS | 403 `FORBIDDEN` |
| 12 | Logged-out session; live session of a member suspended mid-session; suspended member signing in | PASS | 401, 401, 401 |
| 13 | Every claim cites evidence in the same reply; no backed claim means `insufficient_evidence` | PASS | Checked on every department reply |
| 14 | Projects module switched off for Engineering | PASS | No engineering tool offered or run; module restored |
| 15 | Latency and memory | Recorded | See below |

**Latency** (all 87 Qwen calls, warm, from `core.ai_runs`):

| Stage | Average | p95 |
|---|---|---|
| Plan | 1.1 s | 1.7 s |
| Answer | 2.4 s | 4.1 s |

End to end, an assistant reply took 1.8-5.3 s. There were no timeouts and no provider errors.

**Memory:** the GPU held 6.5 of 8.2 GB with Qwen (5.47 GB) and bge-m3 (0.66 GB) loaded. No other generative model was loaded.

## Defect found and fixed

**Tool arguments were not constrained in the plan schema.**
- **Symptom.** Qwen consistently (5 of 5 runs) planned `it.list_access_requests` with `status: "open"`. The tool accepts only `submitted|approved|rejected|cancelled|proposed`, so the call was dropped and IT questions fell back to document search. The same pattern dropped `finance.list_purchase_requests` ("pending").
- **Cause.** The constrained plan schema fixed the tool **name** but left `arguments` as a free object, and the prompt showed only "status: string".
- **Fix** (`ai/orchestrator.py`, `ai/tools.py`):
  - the plan schema is now a union of one variant per permitted tool, each carrying that tool's own argument schema;
  - the prompt lists the allowed values;
  - the two list tools state which status means open.
- **After the fix,** real Qwen planned `status: "proposed"` in 3 of 3 runs and the tool ran. A regression test was added: `test_plan_schema_constrains_each_tools_arguments`.
- **Full suite: 64 passed.**

## Observations, not failures

- **Unsupported claims.** Several replies contain one `unsupported` claim alongside verified ones, typically a negated or causal sentence. The verifier labels them correctly. Block D's information needs and sufficiency gate address answer quality.
- **The owner reply varies between runs.** In 1 of 5 repeated runs the reply was `verified`; in the others the only claim was a causal sentence, correctly marked `unsupported`.
- **One-model decision.** The seeded routes send Engineering `heavy` work to `qwen2.5-coder:7b`, a second generative model. The smoke test used `general` only. Under the one-model decision this default route should point to `qwen2.5:7b`. That needs a decision, because it changes the seeded configuration.

---

# Block D and AI-authored reports — acceptance report

Recorded 2026-10-23. **Status: developer-tested, not accepted.** Block D misses two of its own release gates
(multi-hop recall, conflict handling) on the development set, and the independent sealed evaluation has not been
run. Reports and the chat redesign meet their functional and security checks but share that evaluation gap.
Measured numbers, history and method: [EVALUATION.md](EVALUATION.md). Report rules: [REPORTS.md](REPORTS.md).

**Environment:** Windows 11, WSL Ubuntu with PostgreSQL 18 (tests, disposable `abteilo_test` / `abteilo_browser`),
Docker Desktop with PostgreSQL 17.11 (isolated verification project), Python 3.13.2, Node 22, Ollama 0.31.1 with
`qwen2.5:7b` (every generative call) and `bge-m3`. No paid or external model was called.

## Results

| Area | Check | Result |
|---|---|---|
| Tests | Backend suite against PostgreSQL | **96 passed** |
| | Frontend production build | Passes |
| | Deterministic L1 probes | 7/7 |
| Block D (real Qwen, 40 cases × 3) | C2 / C1 / C0 outcome accuracy | 112/120 · 108/120 · 86/120 (pass 11) |
| | Unauthorised disclosures, false causes verified, stale facts, errors | 0 / 0 / 0 / 0 in every configuration |
| | Streamed C2 | 37/40; first verified content p50 3.8 s |
| | Release gates | Multi-hop recall 0.50 (gate 0.85) and conflict 1/3 (gate 1.00) **not met** |
| Reports (real Qwen, 5 kinds × 3) | Records in scope present | 51/51 |
| | Wrong status / quantity / person in bodies | 0 / 0 / 0 |
| | Unchecked statements in bodies; causal next steps | 0; 0 |
| | Leaks in documents / event streams; stored source content | 0 / 0; 0 |
| | Requests without permission | 2/2 refused (403, nothing stored) |
| Report security (tests) | Owner-private (other user and company owner: 404) | Pass |
| | Download after role revocation | 403 `REPORT_ACCESS_REVOKED` |
| | Download after a cited document becomes confidential to another department | 403; the user's other reports still download |
| | Disabled membership; disabled account | 401; 401 |
| | Expiry: unreadable at once; retention worker deletes; audited | Pass |
| | Cancelled generation stores nothing; run recorded `CLIENT_CANCELLED` | Pass |
| | Answer export labelled as a past answer; re-checks tools and sources | Pass; 403 `EXPORT_ACCESS_REVOKED` after revocation |
| | Hostile model text (`<b>`, `<para>`, `&`) and Unicode in PDF/Word; pagination | Rendered as text; no failure |
| | Reports checked by older verification rules say so | Pass |
| Migration `0009` | PostgreSQL 18 (tests): clean install base → head every session; upgrade 0008 → 0009 with demo data; downgrade refused while report-stage usage exists | Pass |
| | PostgreSQL 17.11 (Docker project `abteilo-verify-reports`, own volume, image `abteilo-app:verify-reports`): clean install base → 0009 | Pass |
| | PG17 upgrade 0008 → 0009 with seeded data: all row counts identical | Pass |
| | PG17 `conv.reports`: RLS enabled and forced, owner-scoped policy, grants exactly SELECT/INSERT/DELETE, runtime role without BYPASSRLS | Pass |
| | PG17 end to end: real Qwen report in 13.8 s, PDF and Word; owner 404; download after API restart; revoked 403; expired report purged by the retention job, audited | Pass |
| | Development stack (`abteilo_pgdata`, `abteilo-app:local`) untouched and healthy | Confirmed |

## Browser acceptance (real Qwen, disposable `abteilo_browser`)

| Check | Result |
|---|---|
| Create report from the chat: stages Collecting → Writing → Checking; card with title, Complete, expiry; screen-reader status "Report ready to download." | Pass (15.4 s) |
| Download PDF (45 KB) and Word (38 KB) from the card | Pass |
| Download after role revocation | Card shows "You no longer have access…" as an alert; no file saved |
| **Stop while the report is being written** | **Defect found and fixed** (below). After the fix: run recorded `CLIENT_CANCELLED` in the same second as Stop; no report and no chat message stored; Send returns |
| Recovery: a new report right after Stop | Completes normally (15.2 s): the generation slot was released |
| Owner console (9 tabs), Engineering Projects, Finance Budgets & approvals, HR Leave management, IT Access management | Render and work for their users |
| Light / Dark / System themes | Applied, remembered, `aria-checked` set; System follows the OS |
| Keyboard order in the chat (Message → Model → Create report → Send); visible focus ring; targets at least 24 px; contrast at least 4.5:1 on every text/background pair | Pass |
| Phone width (375 px): no horizontal scroll; the chat gets a full screen height | Pass |
| Streaming of answers (verified claims progressively; disconnect cancels) | Pass (recorded 2026-10-20, HTTP 16/16, unchanged code path) |

## Defects found and fixed in this cycle

| Defect | How it was found | Fix |
|---|---|---|
| Stop during report writing did not cancel the model (it finished; nothing stored) | Browser acceptance | Report generation emits progress events so the HTTP layer notices a disconnect while the model writes; regression test |
| "There is no document mentioning …" was `cited` (E11) | Real-Qwen pass 6 | Bare "no", "none", "without", "ohne", "nichts" are negations (INT-04d) |
| The documented rule "Nobody may approve their own request" became unsupported (F4) | Pass 7 | A negative that a cited document states may be cited; records never prove absence |
| "Project A17 is assigned to Jonas Becker, due 2026-11-14" verified (the task's person and date) | Manual review of a real report | Facts must sit on the subject's own line (INT-04b) |
| "B24 is active but stalled waiting for PLC access" verified (document narrative riding on a record status) | Same review | No unrecorded wording on a verified claim (INT-04c, `narrative_beyond_record`) |
| Paraphrased subjects treated inconsistently between status and number/person checks | Pass 9 streamed E8 | One rule: a paraphrase that fits exactly one record may give `cited`, never `verified` |
| A paraphrase fitting several records, or a person without a named subject, could borrow facts; a similar record could replace an exactly named one | Reviewer requirement; new regression tests | Ambiguity guard, `person_without_subject`, exact names never swapped |
| Report labels from older rules could look current | Reviewer requirement | Verifier version stored with every report and answer; old documents say so |
| Four defects in the independent report checker | Reviewing its flags | See EVALUATION.md; each with a self-test case |
| PDF date columns wrapped; a duplicated word in file names; contrast 4.48:1 on one pair; small targets; glyph icons | UI review (ui-ux-pro-max) and the real report | Content-based column widths; file names; colour tokens; target sizes; SVG icons |

## Known failures and limits (not fixed in this cycle)

- **Multi-hop recall 0.50** and **E2** (the resolver does not resolve "project A17"): Block D gate not met.
- **Conflict handling 1/3 in pass 11 (E7):** Qwen answers the conflicting-status question with an unrecorded
  cause; the verifier rejects it, so nothing supported remains. Gate not met.
- **I4:** a generic policy sentence is cited as an answer to an unanswerable question (2/3 runs).
- **I1 recall cost** of the new identity guard: a true statement naming only a request's proposer is rejected.
  Proposed narrow fix (not implemented, not measured): accept such a statement as `cited` when exactly one record
  line holds that person.
- **Thin report narrative:** about 2.8 checked statements per report; Engineering and company summaries are always
  computed from the records because the model's are rejected. Reports are correct and complete, not yet rich.
- **Run-to-run variance** of about ±3/120 on identical cases: single passes are samples, not precise measurements.
- **Development set only:** 40 cases, written and tuned on by the developer. The sealed set is a proposal.
- **`core.users` and `core.memberships` have no RLS** (login needs them before a company is chosen). Every
  application query scopes them explicitly (reviewed again in this cycle); the evaluation checker had one unscoped
  query. Defence in depth would add a policy or a lint test.
- Reports generated before 2026-10-23 carry older labels; their documents now say so.
- Carried over: the Docker owner role is a superuser (D1); the dev Docker stack is still on revision 0003;
  graph-read log retention needs an organisational decision; the 2-second statement timeout is untested under
  load; no MFA or SSO; the production items in ARCHITECTURE.md are open.

## Notes for the development environment

- The development database (WSL, `abteilo`) is at revision 0008. `run-dev.cmd` applies `0009` on its next start
  (additive: one new table and one widened check constraint).
- Disposable artefacts kept: WSL database `abteilo_browser`; Docker projects `abteilo-verify`,
  `abteilo-verify-fresh` and `abteilo-verify-reports` (stopped, volumes kept); image tags
  `abteilo-app:verify-blockc` and `abteilo-app:verify-reports`. The application needs none of them.

## Roadmap position

| Stage | Status |
|---|---|
| Block A — departments and security | Developer-tested |
| Block B — hybrid retrieval | Developer-tested |
| Block C — knowledge graph | Developer-tested |
| Local Qwen integration | Smoke-tested |
| Block D — unified intelligence | Developer-tested; **two release gates not met**; sealed evaluation outstanding |
| AI-authored PDF/Word reports | Functional and security checks pass; narrative thin; sealed evaluation outstanding |
| Chat redesign and themes | Browser-tested |
| Editing existing Word documents | Design only |
| Production hardening | Outstanding |

---

# Block D stabilisation — acceptance report

Recorded 2026-11-02. **Status: Block D still not accepted.** All release gates were met in pass 13, but not in
an immediate repeat with unchanged code (multi-hop recall 0.833 mean / 0.5 worst against 0.85). The independent
sealed evaluation is prepared, not run. Details: [EVALUATION.md](EVALUATION.md#block-d-stabilisation-2026-10-23-to-2026-11-02).

| Check | Result |
|---|---|
| Backend suite (PostgreSQL 18) | **103 passed** |
| Frontend build | Passes |
| L1 deterministic probes | 7/7 |
| C2 pass 13 / repeat | 120/120 / 118/120 |
| Conflict handling | 3/3 in both |
| Unanswerable abstention | 36/36 in both |
| Unauthorised disclosures; false causes verified | 0; 0 in both (pass 12 had 1 false cause in its streamed run, now fixed) |
| Multi-hop recall | 1.00 / 0.833 (gate 0.85): **not stable** |
| Reports (pass 13) | 51/51 records; 0 wrong status, quantity or person; 0 leaks; 15/15 valid files |
| Sealed tooling | Freeze refuses unfrozen or edited files; an author's seed extension applies through the application (tests) |

**New regression tests:** `tests/test_block_d_stabilization.py` (resolver visibility, deterministic conflicts incl.
quoted document sentences, outdated document claims, claim-specific support, role attribution) and
`tests/test_sealed_tooling.py`; identity cases added to `tests/test_assistant_and_privacy.py`.

**Remaining before acceptance:**
1. An independent author writes and freezes the sealed set (`backend/eval/sealed/README.md`,
   `SEED_FACTS.md`); it is run once with `--sealed`.
2. Multi-hop: more multi-hop cases (two are too few for a 0.85 gate) and planner robustness for F3-type
   questions; current-state coverage is lenient (F5).
3. Carried over: report narrative depth; `core.users` / `core.memberships` without RLS; the Docker owner role
   is a superuser; the dev Docker stack is on revision 0003; production hardening.

**Environment note:** on 2026-10-24 the machine stopped during an evaluation chain (WSL and Ollama down); the
interrupted files are kept and labelled, and the chain was rerun in full on 2026-11-02.

---

# Pre-publication audit — 2026-11-03

Code-quality, dependency and pipeline review before the first push to GitHub. No feature or schema change.

| Check | Result |
|---|---|
| Backend suite (WSL PostgreSQL 18) | **104 passed**, no warnings |
| `ruff check` (backend, eval, migrations, tests) | Clean |
| `pip-audit` | 14 advisories → **0** (FastAPI 0.115.12 → 0.142.4, Starlette pinned 1.7.0, python-multipart 0.0.20 → 0.0.32, pytest 8.3.5 → 9.1.1) |
| `npm audit --omit=dev --audit-level=high` | Passes (react-router-dom 6.28.0 → 6.30.6, vite 5.4.11 → 5.4.21) |
| Frontend typecheck and build | Passes |
| Duplicate code (jscpd, ≥8 lines) | 0.07 % of source; repeated approval buttons extracted |
| Secret scan of publishable files | No keys, tokens or `.env` values outside ignored files |
| Docker image | Builds; non-root; fresh database migrated to 0009; `/readyz` ready, healthcheck healthy, docs off, 401 without session, security headers set |
| SQL built from strings (bandit B608, 15 sites) | Reviewed: only constants or typed-model field names are interpolated; values are always bound. Field-name lists now go through `db.set_clause`, which rejects non-identifiers |

**Changes:** `API_ROUTERS` in `main.py` (FastAPI ≥ 0.138 nests included routers, so the route-coverage security
tests now enumerate the routers instead of `app.routes`); `db.set_clause` with a regression test;
`DecisionButtons` in `components/ui.tsx`; exception chaining in `ai/gateway.py`; CI extended with lint,
dependency audits and a container smoke test; tag-triggered image release to GHCR; SHA-pinned actions;
Dependabot.

**Deferred (not reachable in production):** Vite 5 / esbuild dev-server advisories (fix needs Vite 8) and two
moderate React Router 6 advisories (open redirect through attacker-supplied `<Link>` targets, SSR hydration;
the SPA has no SSR and links only to constants or server UUIDs; fix needs React Router 7).

**Observed once, not reproduced:** `test_changed_and_archived_records_end_outdated_edges` failed a single time
right after WSL cold-started (`valid_to >= valid_from` check rejected `valid_to = now()`); it passed in the three
full runs that followed. Projection runs in that test are sequential, so a WSL clock step is the likely cause. If
it recurs, end edges with `statement_timestamp()` instead of `now()`.
