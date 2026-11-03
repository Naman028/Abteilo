# Governed Word Document Editing: Design

**Status: design only. Implementation is scheduled after Block D. Nothing here is built.**

**Model.** The single local model (`qwen2.5:7b`). The model never touches a file: it proposes typed edit operations, and a backend service applies them to a copy.

## 1. Workflow

```text
Authorise -> read published version -> model proposes typed edits -> service applies them to a NEW draft
-> validate (structure, size, content) -> show diff -> human approves or rejects -> publish as new version
-> index for retrieval and graph
```

- **Drafts are never retrievable.** Search, graph mentions and the assistant read only the document's current published version (RAG-02 unchanged). A draft becomes searchable only when it is published.
- **Nothing is overwritten.** Publishing appends a new immutable version (`core.document_versions`). Earlier versions and their bytes stay.
- **No automatic publishing.** An authorised person with `documents.write` in the owning department decides every draft. AI-produced text is labelled as such until it is published.

## 2. Scope

**Phase 1 (`.docx` only):**

| Task | Operation(s) |
|---|---|
| Draft a new report | `create_document` from a template (title, headings, paragraphs, simple tables) |
| Rewrite selected paragraphs | `replace_paragraph` |
| Summarise a document | Read only. Produces a new draft document, never edits the source. |
| Add an approved section or table | `insert_section_after`, `insert_table_after` |
| Meeting minutes and department reports | `create_document` from evidence (Block D answer plus its evidence references) |

**Out of scope:**
- a browser Word editor or co-authoring;
- tracked changes and comments in the output;
- `.doc`, `.docm` (macros), `.dotx`;
- equations, SmartArt, text boxes;
- editing headers and footers;
- embedded objects.

## 3. Components

| Component | Responsibility |
|---|---|
| `abteilo/docs_service/` package (in the monolith) | Read, validate, apply operations, render the diff. The only code that opens `.docx` bytes. |
| `python-docx` (new dependency, pinned, licence MIT) | Paragraph, run, table and style access |
| Storage | `core.document_files(version_id, sha256, size, media_type, content bytea)` under tenant RLS. Bytes live in PostgreSQL (bounded size), so there are no filesystem paths. A later move to object storage keeps the same interface. |
| Drafts | `core.document_drafts(id, document_id, base_version_id, author_id, origin ['ai','human'], ops jsonb, file_id, status ['proposed','approved','rejected','superseded'], decided_by, decided_at)` |
| Text extraction | Paragraph and table text from the published version, fed to the existing chunk and embedding pipeline. Headings become chunk boundaries. |

## 4. Edit contract (what the model may propose)

The model returns an `EditPlan` under a schema-constrained output, the same mechanism as tool planning:

```python
class ParagraphRef(BaseModel):
    index: int                      # position in the base version's body
    text_sha256: str                # hash of that paragraph's text when it was read

class ReplaceParagraph(BaseModel):
    op: Literal["replace_paragraph"]
    target: ParagraphRef
    text: str = Field(max_length=4000)

class InsertSectionAfter(BaseModel):
    op: Literal["insert_section_after"]
    anchor: ParagraphRef
    heading: str = Field(max_length=200)
    heading_level: Literal[1, 2, 3]
    paragraphs: list[str] = Field(max_length=20)

class InsertTableAfter(BaseModel):
    op: Literal["insert_table_after"]
    anchor: ParagraphRef
    header: list[str] = Field(max_length=10)
    rows: list[list[str]] = Field(max_length=50)

class EditPlan(BaseModel):
    base_version_id: UUID
    rationale: str = Field(max_length=600)
    ops: list[ReplaceParagraph | InsertSectionAfter | InsertTableAfter] = Field(max_length=20)
```

**Validation before applying:**
- **Base version.** `base_version_id` must be the document's current version. A stale plan is rejected with `DRAFT_BASE_OUTDATED`.
- **Targets.** Every `ParagraphRef` must exist, and its `text_sha256` must still match, which guards against concurrent edits.
- **Factual text.** Free text in operations is checked against Block D evidence when the edit asserts business facts. Numbers, statuses and codes must be supported by cited evidence, or the draft is flagged "contains unverified facts".
- **No deletion operation in phase 1.** Removing content is a human edit.

## 5. Preserving formatting

| Operation | Formatting rule |
|---|---|
| `replace_paragraph` | Keeps the paragraph's style and the first run's character formatting. Mixed inline formatting inside a replaced paragraph (a bold word in the middle) is **not** kept, because python-docx `paragraph.text = ...` collapses runs. This is shown in the diff ("formatting simplified"). |
| Insertions | Use the document's own styles (`Heading 1-3`, `Normal`, `Table Grid` if present, else the default). |
| Untouched content | Byte-for-byte unchanged in the draft's XML parts, except the body positions that operations change. Images, headers, footers and section properties are preserved because they are not touched. |

## 6. Safety and limits

- **Inputs.**
  - `.docx` only, checked by content type and package structure.
  - At most 10 MiB compressed, 50 MiB uncompressed in total, and 2 000 zip entries (zip-bomb guard).
  - Rejected: macro-enabled or encrypted packages, external relationships (linked images or templates), `altChunk` imports.
- **Parsing.** XML parsing must not resolve external entities or perform network access. To verify at implementation: python-docx's parser settings, with a test that feeds an XXE payload.
- **Authorisation.**
  - Reading follows document visibility (RAG-01), and opening re-authorises every time (EVD-04).
  - Proposing a draft needs `documents.write` in the owning department; so does deciding one.
  - Drafts are visible only to the author and to those with `documents.write` there.
  - Confidential documents follow `documents.confidential`.
- **Audit.** `documents.draft_created` (with `origin`), `documents.draft_approved`, `documents.draft_rejected` and `documents.version_published` are written in the same transaction. They hold no document content.
- **Retention.** Rejected and superseded drafts are deleted after 30 days by the retention worker. Published versions follow business-record retention (never deleted by the application).

## 7. API (proposed)

| Endpoint | Purpose |
|---|---|
| `POST /api/documents` (multipart, `.docx`) | Upload a new document (extends today's text upload) |
| `POST /api/documents/{id}/drafts` `{instruction}` | The model proposes an `EditPlan`; the service applies it; returns the draft and its diff |
| `GET /api/documents/{id}/drafts/{draft_id}` | Draft, diff and validation flags |
| `POST /api/documents/{id}/drafts/{draft_id}/decision` `{approve\|reject}` | Human decision; approval publishes a new version |
| `GET /api/documents/{id}/versions/{v}/file` | Download (re-authorised) |

## 8. Tests required at implementation

- Operations applied to real `.docx` fixtures: styles, tables and images are preserved, and the result opens cleanly. Each result is checked by re-parsing and by a schema check of `document.xml`.
- A stale `base_version_id` or hash mismatch is rejected.
- Drafts never appear in search, the graph or the assistant until they are published.
- Approval needs `documents.write`; there is no cross-department or cross-tenant access to drafts.
- XXE, zip-bomb, macro and external-relationship payloads are rejected.
- Audit events are written; the original version's bytes are unchanged after publishing.

## 9. Known limitations

- Mixed inline formatting in replaced paragraphs is simplified.
- No tracked-changes output: the diff is shown in Abteilo, not inside Word.
- No headers or footers, comments, fields (tables of contents, cross-references) or complex layout editing.
- Legacy `.doc` is not supported. Convert it outside Abteilo first.
