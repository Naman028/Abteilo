# Retrieval Evaluation (Block B)

Measured on 2026-10-19 with `python -m eval.retrieval --model bge-m3 --model nomic-embed-text`.

- **Machine:** Windows 11, PostgreSQL 18.6 with pgvector 0.8.7 in WSL, Ollama 0.31.1.
- **Raw results:** `backend/eval/results/*.json`.

## Data set

`backend/eval/corpus.json` is a fictional company, separate from the demo seed.

**Documents:** 24 across Engineering, Finance, HR and IT & Security.
- Four are German.
- Two are confidential: the salary bands (HR) and an internal audit (Finance).

**Questions:**
- **Development set:** 38 questions, asked as five different users with real role assignments. Kinds:
  - keyword (6);
  - identifier, i.e. codes and amounts (5);
  - paraphrase with no shared key terms (12);
  - German to German (4);
  - German to English (7);
  - English to German (4).
- **Held-out set:** 8 questions written after all tuning and never used to change the system.
- **Security set:** 5 questions that try to reach documents the asking user must not see.

**Authorisation ground truth** is computed from the corpus metadata (classification, owning department, the user's role). It is independent of the application's own SQL filter. Every result of every question in every mode and at every threshold is checked against it.

## Results (default threshold 0.55)

| Model | Mode | Recall@5 | Recall@10 | MRR@10 | Held-out R@5 / MRR | Search p50 |
|---|---|---|---|---|---|---|
| — | keyword | 0.579 | 0.579 | 0.579 | 0.875 / 0.875 | 0.6 ms |
| **bge-m3** (1024 d) | vector | 1.000 | 1.000 | 0.954 | 1.000 / 1.000 | 1.1 ms |
| **bge-m3** | **hybrid** | **1.000** | **1.000** | **0.961** | **1.000 / 1.000** | 1.9 ms |
| nomic-embed-text (768 d) | vector | 0.842 | 0.921 | 0.770 | 1.000 / 0.833 | 1.1 ms |
| nomic-embed-text | hybrid | 0.842 | 0.921 | 0.783 | 1.000 / 0.833 | 1.8 ms |

Query embedding latency (p50 / p95):
- bge-m3: 393 / 424 ms
- nomic-embed-text: 231 / 271 ms

Indexing all 24 documents with bge-m3 took 17 s on first load.

### By question kind (bge-m3, Recall@5 / MRR@10)

| Kind | Keyword | Vector | Hybrid |
|---|---|---|---|
| Keyword (6) | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| Identifier (5) | 1.00 / 1.00 | 1.00 / 0.85 | 1.00 / 0.90 |
| Paraphrase (12) | 0.42 / 0.42 | 1.00 / 1.00 | 1.00 / 1.00 |
| German (4) | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| German to English (7) | 0.14 / 0.14 | 1.00 / 0.93 | 1.00 / 0.93 |
| English to German (4) | 0.25 / 0.25 | 1.00 / 0.88 | 1.00 / 0.88 |

### Security

**Unauthorised results: 0. Security-set violations: 0.**

This holds for both models, all three modes and all five distance thresholds:
- every one of the 38 + 8 questions;
- every one of the 5 attack questions.

The confidential salary-band and audit documents never appeared for users without the confidential permission in their department.

## Decisions taken from these measurements

1. **Embedding model: `bge-m3`.** nomic-embed-text cannot bridge German and English (German-to-English Recall@5 0.43 against 1.00). That matters for a German company with mixed-language documents. bge-m3 costs about 160 ms more per query.
2. **Default mode: hybrid.**
   - It ranks best overall (MRR 0.961).
   - It beats vector-only on exact identifiers (0.90 against 0.85).
   - It leaves a working keyword channel when the embedding model is down (OPS-01).
3. **Distance threshold: 0.55.**

   | Threshold | Hybrid Recall@5 |
   |---|---|
   | 0.45 | 0.92 (loses cross-lingual matches) |
   | 0.55 | 1.00 |
   | 0.65, 0.75, none | 1.00, same as 0.55 |

   0.55 is the tightest setting with full recall. It keeps weakly related chunks out of the assistant's evidence.

## How the system changed during evaluation (development set)

| Step | bge-m3 hybrid MRR | Change |
|---|---|---|
| First run | 0.817 | Keyword side ORed every word, including "how", "what" and "wie", so noise outranked good semantic hits |
| English/German stopword filter | 0.904 | Question words no longer match |
| AND-matching for the keyword side inside hybrid | 0.961 | Keyword search becomes a precision signal; semantics come from vectors. Keyword-only search and the outage fallback keep OR-matching for recall. |

Also found and fixed during Block B: chunks were first embedded without their document title. A chunk often lacks the topic word ("Salary Bands" appears only in the title). Each chunk is now embedded as `title + content`.

## Limitations

- **Small corpus.** 24 documents and 46 questions demonstrate the method and catch regressions. They do not predict quality on a real company's documents. Re-run the harness on representative data before relying on these numbers.
- **Held-out set is small** (8 questions) and easier than the development set.
- **No reranker.** The design keeps it behind a flag until it beats rank fusion on this harness; it was not built.
- **Exact search.** pgvector uses exact nearest-neighbour search, which is fine at this scale. An HNSW index with filtered scanning is the documented next step if latency grows.
- **Holding a connection during embedding.** The assistant embeds the query inside its tool transaction, which holds a pooled connection for about 0.4 s. The search endpoint embeds before opening its transaction.

---

## Follow-up (2026-10-20): answerability of unanswerable questions

This is a separate measurement. The results above are unchanged.

### Method

- **Questions.** `backend/eval/corpus.json` gained a `no_answer` set: 10 questions written on 2026-10-20 **before** any measurement, that no document answers. Examples: the canteen menu (English and German), leasing a company car, the share price target, the summer party, a gym subsidy (German), business cards, visitor parking, a football tournament, dogs in the office. Each is asked as a user from a different department.
- **Use.** They are measured, never tuned on.
- **Correct behaviour.** Retrieval returns nothing for the question ("abstains").
- **Run.** The same harness (`python -m eval.retrieval --model bge-m3 --model nomic-embed-text`), run at 2026-10-19 23:40 UTC (2026-10-20 01:40 CEST), on the same machine and corpus as above.
- **Raw results:** `backend/eval/results/*.json`, under `sweep.<threshold>.<mode>.no_answer`.
- **Security in the same run:** 0 unauthorised results, 0 security-set violations.

### Results: unanswerable questions abstained (of 10), with Recall@5 on the development set

| Threshold | bge-m3 hybrid | bge-m3 vector | nomic hybrid | Keyword only |
|---|---|---|---|---|
| 0.45 | **10** (R@5 0.92) | 10 (0.84) | 5 (0.79) | — |
| **0.55 (default)** | **5** (R@5 1.00) | 5 (1.00) | 0 (0.84) | 8 (0.58) |
| 0.65 | 2 (1.00) | 2 (1.00) | 0 (0.84) | — |
| 0.75 / none | 0 (1.00) | 0 (1.00) | 0 (0.84) | — |

At the default, bge-m3 hybrid returned a passage for 5 questions:

| Question | Matched | Distance |
|---|---|---|
| canteen opening hours (German) | Remote Work Policy | 0.48 |
| dog in the office | Remote Work Policy | 0.49 |
| business cards | Purchasing Policy | 0.51 |
| gym subsidy (German) | Purchasing Policy | 0.54 |
| company car lease | Leave Policy | 0.54 |

### What this shows

- **No threshold separates the two.** Answerable and unanswerable questions overlap in distance. The setting that abstains on all 10 (0.45) loses cross-lingual answers (Recall@5 0.92). The setting with full recall (0.55) answers half of the unanswerable ones.
- **Answerability must be decided by evidence support.** It cannot be decided by retrieval similarity. This measures the retrieval stage only. In the assistant, those passages still have to support a verified or cited claim, so an end-to-end answer is less likely than these numbers suggest. That end-to-end rate was **not measured** here.
- **Block D measures it end to end.** It makes evidence support the explicit rule: information needs, a sufficiency gate, and an `insufficient` outcome. See `BLOCK_D_DESIGN.md` §3 and §6, whose release gate is at least 0.90 abstention end to end on a sealed split.

The threshold stays at 0.55. It optimises retrieval recall, which is its job.

---

# Block D: unified intelligence evaluation (real Qwen)

**Development set, not sealed.** The 40 cases in `backend/eval/intelligence_cases.json` were written, inspected
and tuned on by the developer. Every number below describes this development set only. An independent sealed set
is proposed at the end of this document and has **not** been run.

## Method

- **Harness:** `python -m eval.intelligence --level L2 --config C0|C1|C2 --runs 3` and `--stream` (one run).
  Resets and seeds only the disposable test database (`eval/intelligence_seed.py`: BINA with four departments
  and an owner, a second company Nordwerk, a suspended member, a superseded and a contradicting document,
  reviewed and structural graph edges).
- **Model:** `qwen2.5:7b` through Ollama 0.31.1 for every call; `bge-m3` embeddings. Same machine as the Step 1
  smoke test (RTX 4060 Laptop, 8 GB). No paid or external model.
- **Configurations:** C0 documents only, no gate; C1 records + documents, no graph, no gate (approximates the
  pre-Block-D assistant); C2 Block D as built.
- **Denominators:** 40 cases × 3 runs = **120 case-runs** per configuration; the streamed run is 40 case-runs.
  Category denominators are cases × 3 (for example unanswerable: 12 cases → 36).
- **Ground truth** is judged by the harness from the case definitions, not by the code under test.
- **Preflight:** since 2026-10-23 every run first checks that Qwen answers. One earlier chain ran while Ollama was
  stopped (every call `MODEL_UNAVAILABLE`); its files were discarded and are not part of any table here.

## Final results: pass 11 (2026-10-23, final code)

| Measure | C2 (Block D) | C1 | C0 |
|---|---|---|---|
| Outcome accuracy, mean / worst run | **112/120 = 0.933** / 0.90 | 108/120 = 0.90 / 0.875 | 86/120 = 0.717 / 0.70 |
| Unanswerable correctly abstained | 34/36 = 0.945 | 35/36 = 0.972 | 33/36 = 0.917 |
| Multi-hop required-evidence recall | 0.50 | 0.667 | 0 |
| Required-evidence recall, all cases | 0.947 | 0.86 | 0.263 |
| Single-source + policy accuracy | 0.867 | 0.80 | 0.333 |
| Conflict handling | 1/3 | 1/3 | 3/3 |
| Unauthorised disclosures | **0** | 0 | 0 |
| False causes verified | **0** | 0 | 0 |
| Stale facts asserted | 0 | 0 | 0 |
| Errors | 0 | 0 | 0 |
| Latency p50 / p95 | 4.3 s / 6.6 s | 4.5 s / 6.2 s | 5.0 s / 7.3 s |

**C2 by category** (correct outcome / case-runs): causal false 9/9, causal true 3/3, conflict 1/3, multi-hop 6/6,
owner 6/6, policy 12/12, record 14/18, stale 3/3, suspended 3/3, unanswerable 34/36, unauthorised 21/21.

**C2 streamed** (40 case-runs): 37/40 = 0.925; unanswerable 12/12; 0 disclosures, 0 false causes; first verified
content p50 3.8 s / p95 4.9 s (n = 22), complete p50 4.5 s / p95 6.8 s.

**Deterministic probes (L1):** 7/7.

### Every C2 miss in pass 11, attributed

| Case | Runs | What happened | Cause |
|---|---|---|---|
| E2 (A17 project status) | 3/3 | `insufficient` | Known resolver defect: a planner subject that *contains* the code ("project A17") is not resolved, and the need is typed `relationship`, so the gate refuses although the record holds the answer. |
| E7 (conflicting B24 status) | 2/3 | Qwen answered only "B24 is blocked because …" | Model behaviour; the verifier correctly rejected the unrecorded cause, leaving no supported claim. |
| I4 (business cards, unanswerable) | 2/3 | `answered` from cited purchasing-policy sentences | Known false answer: generic policy sentences are cited and count as answering. |
| I1 (open access requests) | 1/3 (and 1/1 streamed) | "Tim Schulz proposed the access request" rejected | **Measured recall cost of the identity guard** added in pass 11 (`person_without_subject`): access-request lines are named after the person the request is *for*, so a statement naming only the proposer has no named subject. Answered 3/3 in passes 9 and 10. |

E3 (streamed) was rejected for a statement that names the retest exactly while paraphrasing a second task, and
for a negation; both are the conservative rules working.

### Release gates (BLOCK_D_DESIGN.md §6.5), on the development set

| Gate | Result | Status |
|---|---|---|
| 0 unauthorised disclosures | 0 in all configurations and passes since pass 2 | Met |
| 0 verified false causes | 0 since pass 4 | Met |
| Graph reads logged; immediate person invalidation | Tests pass | Met |
| Unanswerable abstention ≥ 0.90 | 0.945 | Met (development set only) |
| Multi-hop recall ≥ 0.85 | **0.50** | **Not met** |
| No single-source regression vs C1 above 0.05 | 0.867 vs 0.80 | Met |
| Conflict handling 1.00 | **1/3** in pass 11 (3/3 in passes 3-8) | **Not met** |

Block D therefore does **not** pass its own release gates, independently of the missing sealed evaluation.

## Run-to-run variance

Passes 9, 10 and 11 ran on cases and data that did not change. C2 moved 117 → 114 → 112. The difference between
passes 9 and 10 (no rule change affecting the misses) came only from model variance: about ±3 case-runs out of
120. Pass 11's single attributable change is I1 (one run). A single pass is therefore not a precise measurement;
the table above is one sample.

## History (labelled checkpoints, kept unedited in `backend/eval/results/`)

| Pass | Date (UTC) | C2 | C1 | C0 | What changed before it |
|---|---|---|---|---|---|
| 1 | 2026-10-20 | 0.675 (3 disclosures) | — | — | First real run; E8 leak of a suspended member's name |
| 2 | 2026-10-20 | 0.897 | — | — | Typed `*_active` flags, strict schema, resolver fixes |
| 3 | 2026-10-20 | 0.917 (**1 false cause**) | 0.883 | 0.742 | Quantity and causality corrections |
| 4 | 2026-10-20 | 0.95 | — | — | Cause-first and German causal markers |
| 5 | 2026-10-20 | 0.958 | 0.867 | 0.75 | Strict identity rule (INT-04a) |
| 6 | 2026-10-21 | 0.942 | 0.90 | 0.75 | Reports added (question flow unchanged); E11 exposed the negation gap |
| 7 | 2026-10-23 | 0.942 | 0.875 | 0.717 | Negation fix; F4 regressed (documented rule rejected) |
| 8 | 2026-10-23 | 0.967 | 0.90 | 0.75 | A negative that a cited document states may be cited |
| 9 | 2026-10-23 | 0.975 | 0.867 | 0.75 | Facts on the subject's line; no narrative riding on a verified fact |
| 10 | 2026-10-23 | 0.95 | 0.867 | 0.758 | Paraphrased subjects give `cited` for numbers and people too |
| **11** | 2026-10-23 | **0.933** | **0.90** | **0.717** | Ambiguity guard, no swapping an exact name, person needs a subject |

Each change was made because a real-model run or a manual review showed a wrong label: never to raise a score.
Two changes lowered measured accuracy (pass 7, pass 11); they were kept because they remove a way to present an
unproven statement as fact.

# Reports evaluation (real Qwen)

**Harness:** `python -m eval.reports --runs 3`. Five report kinds (Engineering, Finance, HR, IT, owner) × 3 runs =
**15 reports**, generated through the streaming chat endpoint and downloaded as PDF and Word. Ground truth is read
with SQL directly from the database, independent of `abteilo/report_data.py`. The checker identifies each written
statement's subject by authoritative keys (project code, task title, request title, role, department; the most
specific wins; two subjects at that level = ambiguous, not judged) and checks its status, every quantity and the
person it names; it runs a self-test before each evaluation.

## Final results (pass 11, `reports-final.json`)

| Measure | Result |
|---|---|
| Records in scope present in the document | **51/51** |
| Reports marked complete (none exceeded the bounds) | 15/15 |
| Valid PDF and Word files | 15/15 |
| Written statements in report bodies | 42 (34 with an identified subject) |
| Wrong status / wrong quantity / wrong person | **0 / 0 / 0** |
| Unchecked (unsupported, outdated) statements in a body | 0 |
| Verified causal statements in a body | 0 |
| Recommended next steps / with a stated cause | 35 / 0 |
| Statements the model wrote that were left out | 23 |
| Summary verified / computed from the records | 7 / 8 |
| Privacy markers in documents / in event streams | 0 / 0 |
| Stored chat evidence items with source content | 0 |
| Requests without permission refused (403, nothing stored) | 2/2 |
| Generation time p50 / max | 7.4 s / 16.8 s |

| Kind | Records found | Body statements | Left out | Summary verified (of 3) | p50 |
|---|---|---|---|---|---|
| Engineering progress | 24/24 | 16 | 7 | 0 | 15.6 s |
| Finance status | 9/9 | 9 | 0 | 1 | 7.4 s |
| Leave requests | 3/3 | 3 | 0 | 3 | 4.7 s |
| Access requests | 3/3 | 3 | 1 | 3 | 4.7 s |
| Company status | 12/12 | 11 | 15 | 0 | 12.9 s |

**What this means.** Completeness and correctness come from the deterministic tables and the verifier. The written
narrative is thin: about 2.8 checked statements per report, and the model's Engineering and company summaries were
rejected in every run, so those reports carry a summary computed from the records. Correct, but not yet the rich
narrative a manager would write.

## Checker corrections (recorded, not hidden)

The independent checker itself had defects, each found by reviewing a flag and each fixed with a self-test case.
A flag was never removed by weakening a rule:

1. A task sentence ("the task … of project A17 is done") was attributed to project A17. Replaced a "skip task
   sentences" shortcut by subject identification with authoritative keys (the reviewer's requirement).
2. Membership counts were taken across companies (`core.memberships` has no RLS); now scoped to the company.
3. Written dates and date ranges ("6 November 2026", "November 2 to 6, 2026") were read as quantities.
4. Responsible people were not judged at all; a person credited to the wrong entity is now a `wrong_person`.

---

# Block D stabilisation (2026-10-23 to 2026-11-02)

**Development set, not sealed** (same 40 cases; same method as above). Baseline: pass 11.

## What changed

| Failure (pass 11) | Root cause found | Fix |
|---|---|---|
| E2 "project A17", F3 "thermal test rig purchase" (multi-hop recall 0.50) | The resolver matched only labels that *contain* the subject; kind words broke code matching | Kind-word normalisation and the longest visible record label inside the subject; departments and people never resolved from a phrase (INT-04g) |
| E7 conflict (1/3) | Whether a conflict surfaced depended on Qwen's wording | Deterministic source comparison, typed `StatusConflict`, a record-built statement for status questions, the document's value marked `outdated` (INT-04f) |
| I4 false policy answer | Coverage matched the need's subject against Qwen's own sentence; a document sentence with a status word was cited regardless of added wording | Claim-specific support: no added wording on document sentences; the question's own terms must be in the cited sources (English questions) (INT-04e) |
| I1 proposer rejected | People were matched to the subject line only | Typed person roles (assignee, requester, proposer) |
| Found during pass 12: a verified sentence with a second, unrecorded causal hop (E5 streamed) | Only the first causal link was checked | `causal_link_partly_recorded`: a further cause in the cause part makes the sentence at most cited |
| Found during pass 12: "engineering tasks" resolved to the Engineering department (F5) | The new fallback resolved hubs | Hubs excluded from the fallback |

## Results

| Measure | Pass 11 (baseline) | Pass 12 | **Pass 13 (final code)** | **Pass 13 repeat (same code)** |
|---|---|---|---|---|
| C2 outcome accuracy (mean / worst) | 112/120 · 0.933 / 0.90 | 119/120 · 0.992 / 0.975 | **120/120 · 1.00 / 1.00** | **118/120 · 0.983 / 0.975** |
| Multi-hop recall (gate ≥ 0.85) | 0.50 | 0.833 (worst 0.5) | **1.00** | **0.833 (worst 0.5)** |
| Conflict handling (gate 1.00) | 1/3 | 3/3 | **3/3** | **3/3** |
| Unanswerable abstention (gate ≥ 0.90) | 0.945 | 1.00 | 1.00 | 1.00 |
| Single-source + policy | 0.867 | 1.00 | 1.00 | 1.00 |
| Unauthorised disclosures | 0 | 0 | 0 | 0 |
| False causes verified | 0 | 0 (C2) / **1 (streamed)** | 0 | 0 |
| C2 streamed | 37/40 | 39/40 | 40/40 | 40/40 (multi-hop recall 0.5) |
| C1 / C0 | 108 / 86 | 103 / 87 | 103 / 86 | — |
| C2 latency p50 / p95 | 4.3 / 6.6 s | 4.6 / 7.1 s | 5.0 / 7.5 s | 5.0 / 7.2 s |
| Reports: records found; wrong status/quantity/person; leaks | 51/51; 0/0/0; 0 | 51/51; 0/0/0; 0 | 51/51; 0/0/0; 0 | — |

C2 by category in pass 13: every category correct (record 18/18, policy 12/12, multi-hop 6/6, conflict 3/3,
causal 12/12, unanswerable 36/36, unauthorised 21/21, owner 6/6, stale 3/3, suspended 3/3).

## Are the release gates met?

**Not reliably.** Pass 13 met every gate; the repeat with unchanged code missed the multi-hop recall gate
(0.833 mean, 0.5 worst run, 0.5 streamed). Remaining misses in the repeat:

- **F3 (multi-hop):** in some runs the planner chooses a relationship subject that matches no record, so the
  required purchase request is never retrieved (in a direct reproduction the planner chose a resolvable subject
  6 of 6 times; in 2 of 4 evaluation runs it did not). With two multi-hop cases, one miss halves a run's recall:
  the metric is coarse, and the gate needs more multi-hop cases to be meaningful.
- **F5 (unauthorised, 2/3):** Sara (Finance) asks which engineering tasks are blocked; the answer states that the
  thermal test rig purchase request is submitted, a fact she may see. No disclosure, but a fact about another
  subject counts as covering a current-state need: current-state coverage is lenient (any subject word).

Both are understood and neither is a security failure. They were not tuned further, as instructed: the next step
is the independent sealed set, not more development-set tuning.

## Checkpoints

`intelligence-L2-*-pass11-baseline.json`, `*-pass12-before-cause-hop-fix.json`, `*-pass13.json`,
`*-C2*-pass13-repeat.json`; reports `reports-pass11-baseline.json`, `reports-pass12-before-cause-hop-fix.json`,
`reports-pass13.json`. An interrupted chain (the machine stopped on 2026-10-24) left
`reports-pass13-interrupted.json` and `intelligence-L2-C2-pass13-interrupted.json`; they are kept but not used.

---

## Proposal: an independently written, sealed evaluation set

**Status: tooling ready (backend/eval/sealed/, see its README); no sealed cases written yet.** The 40 Block D cases (`backend/eval/intelligence_cases.json`) and the report
checks were written and inspected by the same developer who tuned the system on them. They are a **development
set**. None of the results above may be read as a sealed or independent measurement.

**Who writes it.** Someone who has not seen the 40 development cases, the verifier rules or the prompts, for
example the external reviewer or a colleague at BINA. They get only: the seed description (people, projects,
documents, permissions; `eval/intelligence_seed.py` read as data, not as code), the case schema and the
category list below.

**What it contains (target 90 cases, as in BLOCK_D_DESIGN.md §6.1).**

| Category | Cases | Notes |
|---|---|---|
| Single-source record | 10 | Every department, English and German phrasing |
| Policy | 8 | Including German-only questions |
| Multi-hop | 18 | Record + relationship + document across departments |
| True causal / false causal | 6 / 8 | False ones written to tempt: shared tasks, "is blocking" wording, German |
| Conflicting / stale | 6 / 4 | |
| Unanswerable | 14 | Questions near real documents (the current failure mode, I4) |
| Unauthorised | 12 | Cross-department, confidential, other tenant, suspended member, owner limits |
| Owner | 6 | |
| Reports | 10 | One request per report kind, plus revocation, expiry and a partial (over-bound) scope |

**How it is sealed.**
1. The author writes the file outside the repository and sends its SHA-256 hash before any run.
2. The hash and date are recorded in this document before the first run.
3. The harness runs it with `--sealed`, which prints aggregates only (no questions, no answers, no gold).
4. It is run once per release candidate, after tuning is frozen. A failure is fixed on the development set,
   never by reading sealed cases. If a case must be inspected to diagnose a defect, that case moves to the
   development set and is replaced.

**Gates (unchanged from BLOCK_D_DESIGN.md §6.5).** Blocking: 0 unauthorised disclosures, 0 verified false
causes, 0 report leaks. Quality: unanswerable abstention ≥ 0.90, multi-hop recall ≥ 0.85, conflict handling
1.00, no single-source regression against C1 above 0.05; report completeness 1.00 within bounds.

**What is needed to start.** An author, about two working days for 100 cases, and agreement on the hash
procedure. No code changes are required; `--cases <file> --sealed` exists.
