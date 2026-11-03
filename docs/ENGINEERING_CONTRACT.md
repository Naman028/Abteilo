# Abteilo Engineering Contract

Abteilo is a department-aware AI operating layer for a company. Each department
(Engineering, Finance, Human Resources, IT & Security, plus any department the
owner adds later) gets its own workspace, data scope and AI assistant. The owner
console manages departments, people, models, data retention and audit.

This contract fixes what the system accepts, what it returns, and what must
always hold. A change that breaks a rule here is a design change and must update
this document first.

## 1. End goals

1. Employees get answers and complete department work faster, using only data
   they are authorised to see.
2. Every AI answer is traceable to evidence the user may open; unsupported
   claims are visibly marked, never silently presented as fact.
3. The owner can add departments and AI models, route work to them, and track
   usage and cost, without code changes.
4. Company business data is never lost through AI features, chat deletion or
   retention jobs.
5. Personal data is processed according to GDPR and German employment-data
   rules (BDSG, BetrVG); see `PRIVACY_AND_GDPR.md`.

## 2. Inputs and outputs

| Boundary | Input | Output | Rejects with |
|---|---|---|---|
| Login | `{email, password}` | HttpOnly session cookie + `Principal` view | `AUTH_INVALID_CREDENTIALS` (same code for unknown user and wrong password), `AUTH_RATE_LIMITED` |
| Any `/api` call | Session cookie, `X-Abteilo-CSRF: 1` on writes | Typed JSON (Pydantic) | `AUTH_REQUIRED`, `CSRF_REQUIRED`, `FORBIDDEN`, `NOT_FOUND`, `VALIDATION_FAILED`, `CONFLICT` |
| Department module | Typed command (e.g. `CreatePurchaseRequest`) | Typed record view | Invalid state transition -> `CONFLICT` |
| Document upload | UTF-8 text/markdown, <= 1 MiB, department, classification | `DocumentView` with version + chunk count | `DOCUMENT_INVALID_ENCODING`, `DOCUMENT_TOO_LARGE` |
| Assistant | `{conversation_id?, department_id, message (<= 4000 chars), mode: general/heavy}` | `AssistantReply{summary, claims[{text, type, status, reason, evidence_ids}], evidence[{kind, freshness, ...}], status, outcome, missing[], degradations[], model_profile}`. The `/stream` variant returns the same reply as Server-Sent Events: `stage`, `evidence`, `summary`, `claim`, then `final` or `error`. | `MODEL_UNAVAILABLE`, `MODEL_BUSY`, `EXTERNAL_AI_DISABLED`, `FORBIDDEN` |
| Graph read | `kind` + `ref_id` (neighbourhood, depth 1-3) or two entity ids (path, depth 1-4) | `Neighbourhood` / `PathResult` with per-edge authority and evidence | `NOT_FOUND` (missing and hidden alike), `GRAPH_TIMEOUT` |
| Admin console | Typed admin commands | Typed views, aggregated metrics | `FORBIDDEN` for non-holders of the admin permission |

Errors always use `{"error": {"code", "message", "request_id"}}`. They never
contain SQL, stack traces, database URLs, provider keys, file paths, raw model
output or the content of rejected input.

## 3. Invariants

### Identity and authorisation
- **SEC-01** Every protected request has a server-verified principal (session
  token stored only as a SHA-256 hash; sessions expire and can be revoked).
- **SEC-02** Tenant isolation is enforced by PostgreSQL Row-Level Security under
  a non-owner runtime role without `BYPASSRLS`. A missing company context
  returns zero rows (fail closed).
- **SEC-03** Permissions come from role assignments in the database. A
  department-scoped role grants nothing outside its department. The scope of
  an assignment is checked by a composite foreign key, not only by Python.
- **SEC-04** The owner role manages the company but does not automatically read
  department business records or anyone's chats.
- **SEC-05** No user can approve their own purchase or access request.
- **SEC-06** Elevated roles (`company_owner`, `security_admin`) are granted only
  through an access request approved by a holder of `role.elevated.approve`
  (owners), never by an ordinary security administrator, and never directly
  from the people screen. Revoking them needs the same permission.
- **OWN-01** An active company always keeps at least one active owner. Every
  operation that can remove an owner (disabling an account, revoking an owner
  role) first locks the company row (`SELECT ... FOR UPDATE`), then changes,
  then re-checks; concurrent operations are serialised by that lock.
- **SEC-07** Password resets and account disabling revoke all server-side
  sessions; every protected route rejects them afterwards. Sessions are the only
  credential type (no self-contained tokens that could outlive revocation).
- **SEC-08** Transaction-local settings carry tenant and user context, so a
  pooled connection never carries one request's context into the next.

### AI
- **AI-01** Model output is a proposal. Tools run only when they are on the
  department allowlist and the principal holds the tool's permission.
- **AI-02** The model never receives credentials, other users' chats or data
  the principal cannot read. Retrieved documents are passed as data, never as
  instructions.
- **AI-03** External model providers are unusable until the owner enables
  external AI for the company and the provider key is configured server-side.
- **AI-04** Every claim and the summary must cite evidence returned in this
  request. `verified` means a structured fact (number or status) matched a live
  record or aggregate; identifiers only anchor the subject, and a status must
  sit on the same evidence line as its subject. Negated facts are never
  verified. Facts that only a document states, and narrative wording, are at
  most `cited`. Everything else is `unsupported` and shown with its reason.
  Only verified content is fed back into conversation history.
- **EVD-01** Causal explanations ("because", "due to") are never verified by
  word matching: unsupported unless a cited source states the link, and then
  at most `cited`, even when the effect is a verified fact.
- **EVD-02** Live records are authoritative for current state. A claim backed
  only by a document that a live record contradicts is `outdated`.
- **EVD-03** Disagreement is surfaced: records that disagree make a claim
  `conflicting`; a disagreeing document is listed next to a verified claim.
  Every evidence item shows its authority and as-of time.
- **AI-07** Assistant tools that read a department module are offered and run
  only while that module is switched on.
- **AI-05** Every model call is recorded in `ai_runs` (profile, provider, model,
  tokens, latency, status), without message content.
- **AI-06** Model calls, tool calls, history and evidence sizes are bounded.

### Retrieval
- **RAG-01** Keyword and vector search both apply the requesting user's
  visibility rule inside SQL; a restricted chunk is never a candidate,
  whatever its similarity.
- **RAG-02** Every chunk and its embedding belong to one immutable document
  version; only the current version is searched; the application may write
  embeddings and index status but never chunk text (column-level grants).
- **RAG-03** Revocation is immediate: visibility is evaluated from live grants
  and document status at query time, independent of index state.
- **RAG-04** Each document version has a visible index status (pending,
  indexed, failed) with attempts and a safe error code; failures are bounded
  (3 attempts) and can be re-queued by the owner.
- **OPS-01** If the embedding model is unavailable, search degrades to
  keyword-only, reports it, and never widens access.
- **OPS-02** Query embedding (8 s), indexing calls (120 s), candidate lists
  (20) and results (≤ 20) are bounded.
- Vectors from different embedding models are never compared: each chunk
  records the model that produced it, and search filters on the active model.

### Knowledge graph
The graph is derived data: business tables stay the source of truth, and the
projection only reads them. Status: built (migrations `0005`, `0006`);
acceptance evidence in `ACCEPTANCE.md`, Block C.

- **GRAPH-01** Every entity carries the visibility scope of its source record.
  Visibility is decided at query time from the principal's live grants.
- **GRAPH-02** An edge is visible only if both endpoints are visible and, for a
  document-derived edge, the document is visible too. A missing entity and a
  hidden entity return the same `NOT_FOUND`.
- **GRAPH-03** Every edge has evidence: the source record, or the document
  version plus the quoted sentence. The database refuses to commit an edge
  without evidence.
- **GRAPH-04** The projection is idempotent. A re-run with no new records
  changes nothing.
- **GRAPH-05** Edges are ended (`valid_to` + reason), never deleted or reopened;
  entities are deactivated, never deleted.
- **GRAPH-06** Model-suggested relationships are candidates, never traversed.
  Only an authorised reviewer's decision creates an edge, and that edge is
  bound to the document version it was read from.
- **GRAPH-07** Traversal is bounded: depth <= 3 (neighbourhood) or <= 4 (path),
  60 entities, 200 edges, 2 s statement timeout.
- **GRAPH-08** A hidden entity cuts every path through it and is never named.
  Departments and people are hubs: a traversal may reach them but never
  continues through them, so a shared colleague is not a relationship.
- **GRAPH-09** The graph evaluation (`python -m eval.graph`) judges restricted
  cases from their definitions. Any restricted leak blocks a release.
- **GRAPH-10** A failed projection run rolls back as a whole. It never leaves a
  partial graph and never widens access.
- **GRAPH-11 Tenant-safe projection.** Every entity and edge in a company's
  graph comes from that company's records. Cross-company sharing is not in
  scope, so for every graph element x in G(T): Tenant(x) = T. This must hold on
  first build, rebuild, source updates, membership changes and after an
  interrupted job. Enforced in three places:
  - **Projection queries.** Sources without RLS (`core.memberships`,
    `core.users`) are filtered by `core.current_company_id()` explicitly.
  - **Database keys.**
    - A non-quarantined person entity references
      `core.memberships(company_id, user_id)` through the generated
      `person_user_id` column, so a membership in another company does not
      satisfy it.
    - Edges and candidates reference entities by `(company_id, id)`, so a
      cross-company edge cannot be stored.
  - **Quarantine.** Rows that violated this before `0006` are quarantined, not
    deleted:
    - they are inactive and their label is removed;
    - an audit event records the count per company;
    - they can never be active again;
    - the runtime role can neither set nor clear the quarantine.

### Unified intelligence (Block D)
One pipeline combines hybrid document search, the knowledge graph and live records. The design is in `BLOCK_D_DESIGN.md`; what was built is in `ACCEPTANCE.md`, Block D.

- **INT-01** Every retrieval path (documents, graph, record tools) runs under the principal's tenant context and visibility rules inside SQL. The model receives only items the principal could open directly.
- **INT-02** No permission expansion:
  - offered tools are permitted tools, and each call is re-checked at execution;
  - plan arguments are constrained to each tool's own schema;
  - graph subjects are resolved against entities visible to the principal;
  - conversation history is never evidence.
- **INT-03** The answer outcome (`answered`, `partial`, `insufficient`, `conflicting`) comes from verified claims and information-need coverage, never from retrieval scores. If no need can be covered, the model is not asked to answer.
  - A changing quantity (budget, balance, pending count) is covered only by record or aggregate evidence.
  - A fixed documented figure (an entitlement, a limit) is a policy need, covered by the current document version.
- **INT-04** A causal claim is `verified` only through cause-asserting edges (`blocked_by`, `delays`) with record or reviewed authority, at most two long, and only when the effect itself is verified against live records.
  - A dependency (`depends_on`) or an impact (`affects`) is not proof of the actual cause, so such claims are at most `cited` (`dependency_recorded_not_cause`).
  - A quoted source sentence stating the cause also makes it at most `cited`.
  - Structural edges never establish a cause.
  - Causal wording is detected in English and German, in both directions (effect first: "blocked by", "wegen", "verzögert durch"; cause first: "is blocking", "leading to", "führt zu", "blockiert den"). A sentence with causal wording is judged as causal, whatever the model labelled it.
- **INT-04a** Verified subject identity rests on authoritative identifiers only: a project code, or an exact, unambiguous record name that no longer name shadows and no second entity shares. Fuzzy or stemmed matching may only find candidates; a claim whose subject is established only that way is at most `cited` (`subject_identity_not_established`).
  - A paraphrase may stand in for a subject only when exactly one record fits it; a paraphrase that fits several records identifies none (`subject_identity_ambiguous`). A record the claim names exactly is never swapped for a similar one. A responsible person needs a named subject (`person_without_subject`).
  - Responsible people are checked by their structured role on the record (`assignee`, `requested_by`, `proposed_by`; a leave request's employee is its requester): the role a sentence gives a person must be the field that person holds. A person and role held on exactly one record line identify that record; on several, none.
  - A recorded cause verifies a sentence only if the cause part does not assert a further, unrecorded cause ("…which in turn requires …"): at most `cited` (`causal_link_partly_recorded`).
- **INT-04b** Every checked fact (status, number, date, responsible person) must sit on the record line of the subject the claim names, not merely somewhere in the same evidence item: a project is never credited with its task's assignee or due date (`number_not_in_evidence`, `person_not_on_subject_line`).
- **INT-04c** A claim is `verified` only if it adds no substantial wording beyond the authoritative record (at most one content word that the record does not contain, framing words excepted). Otherwise its checked fact holds but the rest is not a recorded fact: at most `cited` (`narrative_beyond_record`).
- **INT-04e** Claim-specific support: a document-cited sentence must not say more than its source (at most one content word the source lacks). For policy and open needs, the cited sources must also contain half of the question's own key terms when the question is in the sources' language; a related passage is not an answer.
- **INT-04f** Conflicts are found by comparing sources, not by the model: a live record's status for a record the question names by code is compared with every status a document states about it (as a document item or as a quoted source sentence). For a status question, one statement built from the records is added unless the model already stated the live value; a cited claim repeating the document's value becomes `outdated`. The typed conflict is returned with the answer.
- **INT-04g** Entity resolution for discovery: kind words are removed ("project A17" → "A17") and, failing an exact or prefix match, the longest visible record label contained in the subject is used; departments and people are never resolved from a phrase, and two records sharing that label resolve nothing.
- **INT-04d** Absence and negation (English and German, including a bare "no", "none", "without", "ohne", "nichts") are never `verified`: a record list that does not show something does not prove it does not exist. A negative that a cited **document** itself states (a documented rule such as "Nobody may approve their own request") may be `cited`.
- **INT-05** Current-state facts come from live records or aggregates. Graph evidence carries statuses re-read from the business tables. Relationship claims from a projection older than 10 minutes, or one whose endpoints have since changed, are at most `cited`.
- **INT-06** Every citation re-authorises when opened. Stored chats keep evidence references only, never source content.
- **INT-07** Bounds:
  - planning: 4 source calls and 4 information needs;
  - evidence to the model: 12 items, 1 500 characters each, 12 000 in total;
  - graph: depth 2 in the assistant, a 2 s statement timeout inside a savepoint (a timeout degrades the request, it never fails it);
  - generation: at most `ABTEILO_MAX_CONCURRENT_GENERATIONS` (default 2) generations at once; others wait `ABTEILO_GENERATION_QUEUE_SECONDS`, then get `MODEL_BUSY`.
- **INT-08** A person is visible in the graph only while their membership and account are active. This is checked at read time, so a suspension hides them at once.
  - **Assistant evidence never names an inactive member.** The modules return a typed `*_active` field from live membership (assignee, requester, employee, target, proposer); the tools render "an inactive member". Name masking stays only as defense in depth.
  - **The Projects view, the system of record, keeps the historical assignment** with an "inactive" marker, for those already allowed to read the project.
- **INT-09** Graph reads (API and assistant) are logged as metadata only: no labels or content. Only `audit.read` holders can export the log (each export is itself audited). The database lets the application delete rows only after 90 days, and only in the retention job.
- **INT-10** Streaming (`POST /api/assistant/messages/stream`, Server-Sent Events) sends a summary or claim only after the same deterministic verification as the one-shot endpoint, with its status. It never sends raw planning output or unverified text as fact. A client disconnect stops generation, and nothing is stored for an unfinished reply.

### Reports (see [REPORTS.md](REPORTS.md))

- **REP-01** A generated report's scope is collected deterministically from the records the requester may see; the model never chooses which records a report covers. Tables come directly from the records.
- **REP-02** Only `verified`, `cited` or `conflicting` statements enter a report body. An unsupported summary is replaced by one computed from the records, never kept. Next steps are labelled suggestions and are dropped if they mention anything absent from the evidence, contradict a status or state a cause.
- **REP-03** A report that could not include every record in scope (bounds, degraded source) is marked PARTIAL and names what is missing. It is never presented as complete.
- **REP-04** Reports are private to their creator (RLS), immutable (no UPDATE grant), bounded (1 MB) and expiring (at most 90 days by a database check; 30 days or the chat retention by default). Deleting the chat deletes them.
- **REP-05** Every download of a report or an answer export re-checks the area permission, the department and every document source. A revoked permission blocks old reports and exports.
- **REP-06** Files are rendered in memory only. Model and document text is treated as text: escaped for PDF markup, built through python-docx for Word. Audit records hold metadata only, never report content.
- **REP-07** Generating a report (current records) and exporting an answer (a past answer, labelled as not re-checked) are separate actions and separate endpoints.
- **REP-08** Every report and answer records the verifier version that checked it (`VERIFIER_VERSION` in `ai/evidence.py`, changed with every rule change). A stored document checked by other rules says so in its Limitations; its labels are never presented as current.

### Data protection and retention
- **DATA-01** Business data (projects, tasks, budgets, purchase requests, leave
  requests, access requests, documents, departments, audit events) cannot be
  deleted by the application: the runtime role holds no `DELETE` privilege on
  those tables. Records are archived, never removed.
- **DATA-02** Audit events are append-only for the runtime role (no `UPDATE`,
  no `DELETE`).
- **DATA-03** Chat content lives in the separate `conv` schema. A user may
  delete their own chats at any time. RLS restricts chat rows to their owner.
- **DATA-04** Chat retention is set by the owner: keep, 30 days or 60 days.
  The retention job deletes only expired chat rows and pseudonymises old
  `ai_runs` user links; it cannot touch business tables.
- **DATA-05** Every consequential action writes an audit event in the same
  transaction as the change.

### Resources
- **RES-01** Request bodies, uploads, chat messages, tool loops, provider
  timeouts and list endpoints all have explicit upper bounds.

## 4. Extensibility rules

- **New department:** the owner creates it in the console from the `custom`
  template (documents + assistant) or a built-in template. Roles are defined
  once per company with `department` scope and reused for every department, so
  a new department needs no new code or roles.
- **New dedicated model:** the owner adds a model profile (provider + model
  name + limits) and routes a department's `general` or `heavy` work to it. No
  code change.
- **New provider type:** implement the `ModelProvider` protocol in
  `abteilo/ai/providers/` and register it. Nothing else changes.
- **New department tool:** add a typed tool in `abteilo/ai/tools.py` with its
  required permission and add it to the department template allowlist.

## 5. Engineering rules

- Typed inputs and outputs at every boundary; no raw dicts across modules; no
  database rows returned directly from the API.
- SQL is written explicitly; the migration is the single source of schema
  truth; invariants are enforced by constraints, grants and RLS where possible.
- Short explicit transactions; state changes and their audit event commit
  together.
- No Redis, Kafka, Celery, Kubernetes or microservices without demonstrated
  need. One FastAPI app, one PostgreSQL, one worker process.
- Configuration only through environment variables (`ABTEILO_*`); no
  platform-specific logic in application code.
- Tests protect distinct invariants against real PostgreSQL. No sleeps for
  synchronisation, no cosmetic parameter matrices.
- Pinned dependency versions; upgrades go through the full test suite.
