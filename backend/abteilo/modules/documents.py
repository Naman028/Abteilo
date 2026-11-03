"""Department documents: versioned, classified, hybrid (keyword + semantic) search.

Visibility:
- company:      every active member of the company
- department:   holders of documents.read in the owning department
- confidential: holders of documents.confidential.read in the owning department
Documents are archived, never deleted (DATA-01). Every upload creates an
immutable version; only the current version is searchable (RAG-02).

Search (RAG-01, OPS-01):
1. keyword candidates: PostgreSQL full-text search over chunk text and title;
2. vector candidates: cosine distance over chunk embeddings of the active model;
3. Reciprocal Rank Fusion of both ranked lists.
Only vectors of the active model and of the query's size are compared (RAG-05).
The visibility predicate is part of both SQL queries, so a restricted chunk is
never a candidate, whatever its similarity. If the query cannot be embedded,
search degrades to keyword-only and says so; it never widens access.
"""

import hashlib
import re
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from pydantic import BaseModel, StringConstraints
from sqlalchemy import Connection, text

from abteilo import audit
from abteilo.ai.embeddings import EmbeddingError, get_embedder, to_pgvector
from abteilo.authz import Principal
from abteilo.config import get_settings
from abteilo.deps import current_principal, load_department, tx
from abteilo.errors import AppError, NotFound

router = APIRouter(prefix="/api", tags=["documents"])

MAX_DOCUMENT_BYTES = 1_048_576
MAX_CHUNK_CHARS = 1200
RRF_K = 60
CANDIDATES = 20
Classification = Literal["company", "department", "confidential"]
SearchMode = Literal["hybrid", "keyword", "vector"]


class DocumentTooLarge(AppError):
    status_code, code, message = 413, "DOCUMENT_TOO_LARGE", "Documents are limited to 1 MiB."


class DocumentInvalidEncoding(AppError):
    status_code, code, message = 422, "DOCUMENT_INVALID_ENCODING", "Documents must be UTF-8 text or Markdown."


class DocumentView(BaseModel):
    id: UUID
    department_id: UUID
    department_name: str
    title: str
    classification: Classification
    current_version: int
    updated_at: datetime
    index_status: Literal["pending", "indexed", "failed"]


class DocumentContent(DocumentView):
    content: str
    content_sha256: str


class SearchHit(BaseModel):
    chunk_id: UUID
    document_id: UUID
    document_title: str
    department_name: str
    version: int
    chunk_index: int
    content: str
    version_created_at: datetime
    match: Literal["keyword", "semantic", "both"]
    score: float                 # Reciprocal Rank Fusion score
    keyword_rank: int | None     # 1-based position in the keyword list
    vector_rank: int | None      # 1-based position in the vector list
    distance: float | None       # cosine distance of the vector match


class SearchResult(BaseModel):
    hits: list[SearchHit]
    mode: Literal["hybrid", "keyword", "vector"]
    degraded: str | None         # e.g. EMBEDDING_UNAVAILABLE when hybrid fell back to keyword
    embedding_model: str | None


def _visibility(p: Principal, alias: str = "d") -> tuple[str, dict]:
    clause = (
        f"{alias}.status = 'active' AND ({alias}.classification = 'company'"
        f" OR ({alias}.classification = 'department' AND {alias}.department_id = ANY(CAST(:read_depts AS uuid[])))"
        f" OR ({alias}.classification = 'confidential' AND {alias}.department_id = ANY(CAST(:conf_depts AS uuid[]))))"
    )
    return clause, {
        "read_depts": [str(x) for x in p.departments_with("documents.read")],
        "conf_depts": [str(x) for x in p.departments_with("documents.confidential.read")],
    }


_DOC_COLUMNS = ("d.id, d.department_id, dep.name AS department_name, d.title, d.classification, d.current_version,"
                " d.updated_at, cv.index_status")
_DOC_FROM = (" FROM core.documents d JOIN core.departments dep ON dep.company_id = d.company_id AND dep.id = d.department_id"
             " JOIN core.document_versions cv ON cv.document_id = d.id AND cv.version = d.current_version")
_DOC_SELECT = f"SELECT {_DOC_COLUMNS}{_DOC_FROM}"


def chunk(content: str) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", content) if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        while len(para) > MAX_CHUNK_CHARS:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(para[:MAX_CHUNK_CHARS])
            para = para[MAX_CHUNK_CHARS:]
        if len(current) + len(para) + 2 > MAX_CHUNK_CHARS and current:
            chunks.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        chunks.append(current)
    return chunks


def _check_content(content: str) -> bytes:
    raw = content.encode("utf-8")
    if not raw.strip():
        raise DocumentInvalidEncoding("The document is empty.")
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise DocumentTooLarge()
    return raw


def _insert_version(conn: Connection, p: Principal, doc_id: UUID, version: int, content: str) -> None:
    """Immutable version + chunks; the indexer embeds it later (index_status = pending)."""
    raw = _check_content(content)
    version_id = uuid4()
    conn.execute(
        text("INSERT INTO core.document_versions (id, company_id, document_id, version, content, content_sha256,"
             " size_bytes, created_by) VALUES (:id, core.current_company_id(), :doc, :ver, :content, :sha, :size, :u)"),
        {"id": version_id, "doc": doc_id, "ver": version, "content": content, "sha": hashlib.sha256(raw).hexdigest(),
         "size": len(raw), "u": p.user_id},
    )
    for i, piece in enumerate(chunk(content)):
        conn.execute(
            text("INSERT INTO core.document_chunks (id, company_id, document_id, version_id, chunk_index, content)"
                 " VALUES (:id, core.current_company_id(), :doc, :v, :i, :c)"),
            {"id": uuid4(), "doc": doc_id, "v": version_id, "i": i, "c": piece},
        )


def store_document(
    conn: Connection, p: Principal, department_id: UUID, title: str, classification: str, content: str
) -> UUID:
    p.require("documents.write", department_id)
    raw = _check_content(content)
    doc_id = uuid4()
    conn.execute(
        text("INSERT INTO core.documents (id, company_id, department_id, title, classification, created_by)"
             " VALUES (:id, core.current_company_id(), :d, :t, :c, :u)"),
        {"id": doc_id, "d": department_id, "t": title, "c": classification, "u": p.user_id},
    )
    _insert_version(conn, p, doc_id, 1, content)
    audit.record(conn, p.company_id, p.user_id, "documents.uploaded", "document", doc_id,
                 {"classification": classification, "size_bytes": len(raw)})
    return doc_id


def add_version(conn: Connection, p: Principal, document_id: UUID, content: str) -> None:
    doc = open_document(conn, p, document_id)
    p.require("documents.write", doc.department_id)
    row = conn.execute(text("SELECT current_version FROM core.documents WHERE id = :id FOR UPDATE"),
                       {"id": document_id}).one()
    _insert_version(conn, p, document_id, row.current_version + 1, content)
    conn.execute(text("UPDATE core.documents SET current_version = current_version + 1, updated_at = now() WHERE id = :id"),
                 {"id": document_id})
    audit.record(conn, p.company_id, p.user_id, "documents.version_added", "document", document_id,
                 {"version": row.current_version + 1})


def open_document(conn: Connection, p: Principal, document_id: UUID) -> DocumentContent:
    """Permission is re-checked every time a document is opened (evidence invariant)."""
    clause, params = _visibility(p)
    row = conn.execute(
        text(f"SELECT {_DOC_COLUMNS}, cv.content, cv.content_sha256{_DOC_FROM} WHERE d.id = :id AND {clause}"),
        {"id": document_id, **params},
    ).mappings().first()
    if row is None:
        raise NotFound()
    return DocumentContent(**row)


# --- hybrid search ------------------------------------------------------------------------------
_HIT_SELECT = (
    "SELECT c.id AS chunk_id, d.id AS document_id, d.title AS document_title, dep.name AS department_name, v.version,"
    " c.chunk_index, c.content, v.created_at AS version_created_at"
)
_HIT_FROM = (
    " FROM core.document_chunks c"
    " JOIN core.document_versions v ON v.id = c.version_id"
    " JOIN core.documents d ON d.id = c.document_id AND d.current_version = v.version"
    " JOIN core.departments dep ON dep.company_id = d.company_id AND dep.id = d.department_id"
)
_DEPT_FILTER = " AND (CAST(:dept AS uuid) IS NULL OR d.department_id = :dept OR d.classification = 'company')"


# Question words and fillers (English and German). Without this, "how"/"wie" match nearly every
# chunk and the keyword list adds noise to rank fusion (measured in eval/retrieval.py).
_STOPWORDS = frozenset("""
a an and are as at be been before by can did do does for from get goes has have how i if in is it its many may
much my need of often on or our own per should that the there this to was we what when which who why will with
you your ab auf bei bis das dem den der des die darf ein eine einem einen einer für habe hat ich im in ist kann
mein meine mit muss müssen oder pro sie sind und unser von wann warum was welche wer werden wie wir wird zu zum zur
""".split())


def _keyword_candidates(conn: Connection, p: Principal, query: str, department_id: UUID | None,
                        require_all_terms: bool = False) -> list[dict]:
    """OR-matching maximises recall (keyword-only search, fallback). Inside hybrid search the keyword
    list is a precision signal: every meaningful term must match (AND), and semantics come from vectors."""
    terms = [t for t in re.findall(r"\w{2,}", query.lower()) if t not in _STOPWORDS][:12]
    if not terms:
        return []
    clause, params = _visibility(p)
    return [dict(r) for r in conn.execute(
        text(f"{_HIT_SELECT}{_HIT_FROM}, websearch_to_tsquery('simple', :q) q"
             f" WHERE (c.tsv || to_tsvector('simple', d.title)) @@ q AND {clause}{_DEPT_FILTER}"
             " ORDER BY ts_rank(c.tsv || to_tsvector('simple', d.title), q) DESC, c.id LIMIT :n"),
        {"q": " ".join(terms) if require_all_terms else " or ".join(terms), "dept": department_id, "n": CANDIDATES, **params},
    ).mappings()]


def _vector_candidates(conn: Connection, p: Principal, vector: list[float], model: str,
                       department_id: UUID | None) -> list[dict]:
    stored = conn.execute(text(
        "SELECT DISTINCT embedding_dims FROM core.document_versions"
        " WHERE embedding_model = :m AND index_status = 'indexed' AND embedding_dims IS NOT NULL"
    ), {"m": model}).scalars().all()
    if stored and len(vector) not in stored:
        raise EmbeddingError("EMBEDDING_DIMENSION_MISMATCH")  # degrade to keyword search, never a database error
    clause, params = _visibility(p)
    return [dict(r) for r in conn.execute(
        text(f"{_HIT_SELECT}, c.embedding <=> CAST(:qv AS vector) AS distance{_HIT_FROM}"
             f" WHERE c.embedding_model = :model AND c.embedding_dims = :dims AND {clause}{_DEPT_FILTER}"
             " AND c.embedding <=> CAST(:qv AS vector) <= :max_d"
             " ORDER BY distance, c.id LIMIT :n"),
        {"qv": to_pgvector(vector), "model": model, "dims": len(vector), "max_d": get_settings().vector_max_distance,
         "dept": department_id, "n": CANDIDATES, **params},
    ).mappings()]


def fuse(keyword: list[dict], vector: list[dict], limit: int) -> list[SearchHit]:
    """Reciprocal Rank Fusion: score = sum over lists of 1 / (RRF_K + rank)."""
    merged: dict[UUID, dict] = {}
    for rank, row in enumerate(keyword, start=1):
        merged[row["chunk_id"]] = {**row, "keyword_rank": rank, "vector_rank": None, "distance": None,
                                   "score": 1 / (RRF_K + rank)}
    for rank, row in enumerate(vector, start=1):
        entry = merged.setdefault(row["chunk_id"], {**row, "keyword_rank": None, "score": 0.0})
        entry.update(vector_rank=rank, distance=round(float(row["distance"]), 4))
        entry["score"] += 1 / (RRF_K + rank)
    hits = []
    for entry in merged.values():
        match = "both" if entry["keyword_rank"] and entry["vector_rank"] else ("keyword" if entry["keyword_rank"] else "semantic")
        hits.append(SearchHit(**{k: v for k, v in entry.items() if k in SearchHit.model_fields and k != "match"},
                              match=match))
    hits.sort(key=lambda h: (-h.score, h.keyword_rank or 10**6, h.vector_rank or 10**6))
    return hits[:limit]


def hybrid_search(conn: Connection, p: Principal, query: str, department_id: UUID | None = None, limit: int = 6,
                  mode: SearchMode = "hybrid", query_vector: list[float] | None = None,
                  query_error: str | None = None) -> SearchResult:
    """`query_vector` / `query_error` let callers embed the query before opening a transaction."""
    limit = min(limit, 20)
    embedder = get_embedder() if mode != "keyword" else None
    keyword: list[dict] = []
    vector: list[dict] = []
    degraded = None
    if mode != "keyword":
        if embedder is None:
            degraded = "EMBEDDINGS_DISABLED"
        elif query_error:
            degraded = query_error
        else:
            try:
                qv = query_vector or embedder.embed_query(query)
                vector = _vector_candidates(conn, p, qv, embedder.model, department_id)
            except EmbeddingError as exc:
                degraded = exc.code  # fall back to keyword-only; access rules are unchanged
    if mode == "keyword" or (mode == "hybrid" and degraded):
        keyword = _keyword_candidates(conn, p, query, department_id)                       # recall-oriented
    elif mode == "hybrid":
        keyword = _keyword_candidates(conn, p, query, department_id, require_all_terms=True)  # precision signal
    effective = "keyword" if mode == "keyword" or (degraded and mode == "hybrid") else mode
    return SearchResult(hits=fuse(keyword, vector, limit), mode=effective, degraded=degraded,
                        embedding_model=embedder.model if embedder and not degraded else None)


def search_documents(conn: Connection, p: Principal, query: str, department_id: UUID | None = None,
                     limit: int = 6) -> list[SearchHit]:
    return hybrid_search(conn, p, query, department_id, limit).hits


# --- routes ------------------------------------------------------------------------------
@router.get("/departments/{department_id}/documents", response_model=list[DocumentView])
def api_list_documents(department_id: UUID, p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        load_department(conn, department_id, "documents")
        clause, params = _visibility(p)
        rows = conn.execute(
            text(f"{_DOC_SELECT} WHERE d.department_id = :d AND {clause} ORDER BY d.updated_at DESC LIMIT 200"),
            {"d": department_id, **params},
        ).mappings()
        return [DocumentView(**r) for r in rows]


async def _read_upload(file: UploadFile) -> str:
    raw = await file.read(MAX_DOCUMENT_BYTES + 1)
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise DocumentTooLarge()
    try:
        content = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise DocumentInvalidEncoding() from exc
    if "\x00" in content:
        raise DocumentInvalidEncoding()
    return content


@router.post("/departments/{department_id}/documents", response_model=DocumentView, status_code=201)
async def api_upload_document(
    department_id: UUID,
    title: Annotated[str, Form(min_length=2, max_length=200)],
    classification: Annotated[Classification, Form()],
    file: UploadFile = File(...),
    p: Principal = Depends(current_principal),
):
    content = await _read_upload(file)
    with tx(p) as conn:
        load_department(conn, department_id, "documents")
        doc_id = store_document(conn, p, department_id, title.strip(), classification, content)
        return open_document(conn, p, doc_id)


@router.post("/documents/{document_id}/versions", response_model=DocumentView, status_code=201)
async def api_add_version(document_id: UUID, file: UploadFile = File(...), p: Principal = Depends(current_principal)):
    """Upload a new version. The previous version stays stored but is no longer searched."""
    content = await _read_upload(file)
    with tx(p) as conn:
        add_version(conn, p, document_id, content)
        return open_document(conn, p, document_id)


@router.get("/documents/{document_id}", response_model=DocumentContent)
def api_open_document(document_id: UUID, p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        return open_document(conn, p, document_id)


@router.post("/documents/{document_id}/archive", response_model=DocumentView)
def api_archive_document(document_id: UUID, p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        doc = open_document(conn, p, document_id)
        p.require("documents.write", doc.department_id)
        conn.execute(text("UPDATE core.documents SET status = 'archived', updated_at = now() WHERE id = :id"),
                     {"id": document_id})
        audit.record(conn, p.company_id, p.user_id, "documents.archived", "document", document_id)
        return doc


@router.get("/documents-search", response_model=SearchResult)
def api_search(q: Annotated[str, StringConstraints(min_length=2, max_length=300)], department_id: UUID | None = None,
               mode: SearchMode = Query("hybrid"), p: Principal = Depends(current_principal)):
    # Embed the query before opening a transaction, so no pooled connection waits on the model.
    embedder = get_embedder() if mode != "keyword" else None
    vector, error = None, None
    if embedder is not None:
        try:
            vector = embedder.embed_query(q)
        except EmbeddingError as exc:
            error = exc
    with tx(p) as conn:
        return hybrid_search(conn, p, q, department_id, limit=10, mode=mode, query_vector=vector,
                             query_error=error.code if error else None)
