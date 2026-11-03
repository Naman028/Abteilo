# Reports and answer exports

Status: **functional and developer-tested; not production-hardened.**

Abteilo has two separate document features. They are deliberately different and the UI keeps them apart.

| | A. Generate a report | B. Export an answer |
|---|---|---|
| What it is | A new business deliverable written from the **current** records of an area | A copy of one existing chat answer, as it was given |
| How to start it | Ask in chat ("Create a progress report as PDF", "…as a Word report") or press **Create report** in the composer | **Export answer: PDF · Word** under an answer |
| Data | Collected deterministically: every record in scope | The answer's own verified statements |
| Freshness | "Data as of" the generation time | Labelled "export of a chat answer … not re-checked against current records" |
| Stored | Yes: typed content in `conv.reports`, owner-private, expires | No (rendered from the stored chat reply) |
| Endpoint | `GET /api/reports/{id}?format=pdf\|docx` | `GET /api/conversations/{c}/messages/{m}/export?format=pdf\|docx` |

Editing an existing uploaded Word document is **not** implemented.

## Report kinds

| Workspace | Kind | Covers (all records the requester may see) | Permission (checked at generation and at every download) |
|---|---|---|---|
| Engineering | `engineering_progress` | Every project: status, task table (task, status, responsible, due), recorded blockers and dependencies (record and reviewed relationships), the most relevant current document | `engineering.project.read` in the department |
| Finance | `finance_status` | Budget lines per department and period; purchase requests awaiting a decision and decided | `finance.budget.read` in the department |
| Human Resources | `hr_leave` | Leave requests awaiting a decision | `hr.leave.approve` in the department |
| IT & Security | `it_access` | Access requests awaiting approval and decided | IT access view (`it.access.propose` here or `it.access.approve` company-wide) |
| Owner workspace | `company_status` | Pending approvals, departments, blocked projects, budget pressure | `report.executive.read` company-wide |

All also need `ai.chat.use` and membership of the department (or the owner permission). A custom department
has no report kind; asking there returns `REPORT_NOT_AVAILABLE`, and answers can still be exported.

## What the model does, and what it does not

The model **does not choose the data**. The backend enumerates the records and builds the tables directly from
them, so the tables are complete within the bounds below whatever the model writes. The model is called once
(stage `report` in the AI usage metering) to write:

- a **summary** of two sentences;
- up to 12 **observations**, one fact about one named record each, with evidence ids;
- up to 4 **recommended next steps**, phrased as actions, with evidence ids.

Every observation is verified with the same deterministic verifier as chat answers. Only `verified`, `cited`
(from a source, not machine-proven) and `conflicting` (records disagree) statements enter the document; the
number left out is stated in the document. If the summary cannot be verified, a summary **computed from the
records** replaces it and the document says so. Next steps are labelled "AI suggestions … not decisions or
recorded facts" and are dropped when they mention anything absent from the evidence, contradict a status, or
state any cause (a reason inside a suggestion would read as a fact without being labelled one).

If the model is unavailable, the report is still produced from the records, with a note that no narrative
could be written. If the model is busy (`MODEL_BUSY`), nothing is produced and the chat shows the error.

## Document structure (PDF and Word share one typed `Report`)

1. Title (`<company> — <report>`), scope, requester, generation time, reporting period, **data as of**.
2. **Complete** or **PARTIAL** (with "see Limitations").
3. Executive summary, with its basis when it is computed from the records.
4. Overview table (where the kind has one).
5. Key observations (verified / from source / records disagree, with evidence ids).
6. One section per project or request group: table, recorded blockers and dependencies, observations.
7. Recommended next steps (suggestions).
8. Limitations (data gaps and narrative caveats).
9. Sources with authority and "as of" time.
10. A note on how statements were checked and which model wrote the narrative.

PDF: A4, embedded Vera font (shipped with ReportLab; covers German, €, typographic punctuation), table header
rows repeat across pages, page footer with page number. Characters the font lacks are mapped to a close
equivalent (→ becomes "->"), never a blank box. Word: built with python-docx styles (headings, grid tables,
bullets); all Unicode is kept.

## Bounds and partial reports

- At most 20 sections (projects) and 40 rows per table. If more exist, the report is **PARTIAL** and the
  limitation names what was cut ("Only the first 20 of 23 projects are listed").
- A source that degrades (for example a relationship lookup timing out) also makes the report partial.
- The narrative sees at most 12 evidence items / 12 000 characters, like a chat answer. If more exist, a
  note says so; the tables still list every record, so this does not make the report partial.
- Stored content is limited to 1 MB by a database check.

## Security and lifecycle

- **Ownership.** `conv.reports` has owner-scoped RLS (FORCE): only the person who generated a report can read
  it; not their manager, not the company owner, not another tenant. Report ids are random UUIDs.
- **Re-authorisation at every download.** The report's area permission, the department (active, module on)
  and every document source are checked again. A revoked role or a document the user may no longer open
  makes the download fail with `REPORT_ACCESS_REVOKED` (answer exports: `EXPORT_ACCESS_REVOKED`). An old
  report never outlives a permission.
- **Immutable and expiring.** No UPDATE grant. `expires_at` is at most 30 days (or the company's chat
  retention, if shorter); a database check caps it at 90 days. Expired reports are unreadable at once and
  deleted by the retention worker (audited as `retention.reports_purged`). Deleting the chat deletes its
  reports.
- **No files on disk.** PDF and Word are rendered in memory for each download; there is no file path, no
  upload and nothing to clean up. Responses carry `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`.
- **Untrusted text.** Model and document text is escaped before it reaches ReportLab's paragraph markup, so
  a model writing `<font>` or `<para>` produces those characters, not formatting. Word files are built through
  python-docx (no raw XML).
- **Verifier version.** Each report (and each chat answer) stores the version of the verification rules that
  checked it. A document checked by an earlier version says so under Limitations ("checked with an earlier
  version of the verification rules … generate a new report for current checks"); old labels are never shown
  as current. Reports created before versions were recorded are treated as "an earlier version".
- **Audit.** `report.created` (kind, complete, number of sections), `report.downloaded` (format),
  `assistant.answer_exported` (format). Never report content.
- **Privacy.** Reports name people only where the area's business screens do, and never a suspended or
  disabled member ("an inactive member").

## Known limitations

- Reports are snapshots: a download shows the data as of generation, clearly dated. Generate a new report for
  current data.
- The model's observations are often rejected by the verifier when it combines facts, adds causes or uses
  negations; the tables carry the facts regardless.
- Report kinds are fixed per department template. There is no period selection (for example "Q3 only") yet.
- Stopping a report while the model writes it cancels generation; stopping during data collection lets the
  collection finish (seconds) and then discards it. Nothing is stored for a stopped report.
