"""Block B: hybrid retrieval invariants (RAG-01..04, OPS-01) with a deterministic embedder."""

import hashlib
import math
import re
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from abteilo.ai.embeddings import EmbeddingError, reset_embedder, set_embedder
from abteilo.db import transaction
from abteilo.indexer import MAX_ATTEMPTS, index_all
from abteilo.modules.documents import RRF_K, fuse
from tests.conftest import department_id, owner_conn, user_id

# A tiny "semantic" space: synonyms (including German) map to one concept.
_CONCEPTS = {
    "salary": "pay", "salaries": "pay", "compensation": "pay", "pay": "pay", "band": "range", "bands": "range", "ranges": "range",
    "range": "range", "gehalt": "pay", "gehaltsbänder": "pay", "leave": "leave", "vacation": "leave",
    "holiday": "leave", "holidays": "leave", "urlaub": "leave", "urlaubstage": "leave", "cooling": "thermal",
    "thermal": "thermal", "overheating": "thermal", "heat": "thermal", "temperature": "thermal",
}


_FILLER = {"the", "and", "for", "are", "per", "eur", "with", "that", "this", "our", "what", "which"}


class FakeEmbedder:
    model = "fake-embed"

    def _vec(self, text_: str) -> list[float]:
        v = [0.0] * 32
        for w in re.findall(r"[a-zäöüß]{3,}", text_.lower()):
            if w in _FILLER:
                continue
            concept = _CONCEPTS.get(w, w)
            v[int(hashlib.sha256(concept.encode()).hexdigest(), 16) % 32] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text_):
        return self._vec(text_)


class BrokenEmbedder(FakeEmbedder):
    def embed_documents(self, texts):
        raise EmbeddingError("EMBEDDING_UNAVAILABLE")

    def embed_query(self, text_):
        raise EmbeddingError("EMBEDDING_UNAVAILABLE")


@pytest.fixture()
def fake_embedder(demo):
    embedder = FakeEmbedder()
    set_embedder(embedder)
    index_all(embedder)
    yield embedder
    reset_embedder()


def _search(client, q, mode="hybrid"):
    r = client.get("/api/documents-search", params={"q": q, "mode": mode})
    assert r.status_code == 200, r.text
    return r.json()


def test_semantic_match_finds_paraphrases_but_never_restricted_chunks(login, fake_embedder):
    mila, jonas = login("mila.hoffmann@bina.example"), login("jonas.becker@bina.example")
    # No keyword overlap with "Salary Bands 2026": only the semantic path can find it.
    assert _search(mila, "compensation ranges", "keyword")["hits"] == []
    hits = _search(mila, "compensation ranges")["hits"]
    assert hits and hits[0]["document_title"] == "Salary Bands 2026" and hits[0]["match"] == "semantic"
    # RAG-01: the engineer's query is semantically identical, the chunk is still never a candidate.
    for mode in ("hybrid", "vector"):
        assert all(h["document_title"] != "Salary Bands 2026" for h in _search(jonas, "compensation ranges", mode)["hits"])
    # Cross-lingual concept match through the embedder, permission-filtered as well.
    assert _search(jonas, "Urlaub")["hits"][0]["document_title"] == "Leave Policy"


def test_reciprocal_rank_fusion():
    ids = [uuid4() for _ in range(3)]

    def row(i):
        return {"chunk_id": ids[i], "document_id": ids[i], "document_title": f"d{i}", "department_name": "x",
                "version": 1, "chunk_index": 0, "content": "c", "version_created_at": "2026-10-19T00:00:00Z",
                "distance": 0.1}

    # Chunk 1 is second in both lists, so it beats chunk 0 (first in keyword only) and chunk 2 (first in vector only).
    hits = fuse([row(0), row(1)], [row(2), row(1)], limit=3)
    assert hits[0].chunk_id == ids[1] and hits[0].match == "both"
    assert hits[0].score == pytest.approx(2 / (RRF_K + 2))
    assert {h.match for h in hits[1:]} == {"keyword", "semantic"}
    assert hits[1].score == hits[2].score == pytest.approx(1 / (RRF_K + 1))


def test_only_the_current_version_is_searchable_and_versions_are_immutable(login, demo, fake_embedder):
    lena = login("lena.weber@bina.example")
    eng = department_id(lena, "Engineering")
    doc = lena.post(f"/api/departments/{eng}/documents", data={"title": "Test bench notes", "classification": "department"},
                    files={"file": ("n.md", b"The zebra rig needs calibration.", "text/markdown")}).json()
    assert doc["index_status"] == "pending"                      # RAG-04: visible state before indexing
    index_all(fake_embedder)
    assert lena.get(f"/api/documents/{doc['id']}").json()["index_status"] == "indexed"
    assert _search(lena, "zebra")["hits"][0]["document_title"] == "Test bench notes"
    v2 = lena.post(f"/api/documents/{doc['id']}/versions", files={"file": ("n.md", b"The rig is calibrated.", "text/markdown")})
    assert v2.json()["current_version"] == 2 and v2.json()["index_status"] == "pending"
    assert _search(lena, "zebra")["hits"] == []                    # RAG-02: superseded text is not searched
    with owner_conn() as conn:                                     # ...but the old version is kept
        assert conn.execute(text("SELECT count(*) FROM core.document_versions WHERE document_id = :d"),
                            {"d": doc["id"]}).scalar_one() == 2
    with pytest.raises(ProgrammingError, match="permission denied"):  # chunk text is immutable for the app
        with transaction(demo, user_id("lena.weber@bina.example")) as conn:
            conn.execute(text("UPDATE core.document_chunks SET content = 'tampered'"))


def test_revocation_takes_effect_without_reindexing(login, fake_embedder):
    mila, owner = login("mila.hoffmann@bina.example"), login("owner@bina.example")
    assert _search(mila, "compensation ranges")["hits"][0]["document_title"] == "Salary Bands 2026"
    mila_id = user_id("mila.hoffmann@bina.example")
    with owner_conn() as conn:
        role = conn.execute(text("SELECT id FROM core.role_assignments WHERE user_id = :u AND role_code = 'hr_specialist'"),
                            {"u": mila_id}).scalar_one()
    owner.delete(f"/api/admin/members/{mila_id}/roles/{role}")
    # RAG-03: embeddings are unchanged, but the next query no longer sees the chunk.
    assert all(h["document_title"] != "Salary Bands 2026" for h in _search(mila, "compensation ranges")["hits"])


def test_index_failures_are_bounded_visible_and_retryable(login, demo):
    owner = login("owner@bina.example")
    broken = BrokenEmbedder()
    for _ in range(MAX_ATTEMPTS):
        index_all(broken)
    data = owner.get("/api/admin/data").json()
    failed = sum(d["index_failed"] for d in data["departments"])
    assert failed == 7 and sum(d["index_pending"] for d in data["departments"]) == 0
    assert data["index_errors"] == {"EMBEDDING_UNAVAILABLE": 7}
    assert owner.post("/api/admin/index/retry").json()["requeued"] == 7
    index_all(FakeEmbedder())
    data = owner.get("/api/admin/data").json()
    assert sum(d["index_indexed"] for d in data["departments"]) == 7 and data["index_errors"] == {}


def test_embedding_outage_degrades_to_keyword_without_widening_access(login, fake_embedder):
    jonas = login("jonas.becker@bina.example")
    set_embedder(BrokenEmbedder())
    result = _search(jonas, "salary bands supplier")
    assert result["mode"] == "keyword" and result["degraded"] == "EMBEDDING_UNAVAILABLE"
    assert {h["match"] for h in result["hits"]} <= {"keyword"}
    assert all(h["document_title"] != "Salary Bands 2026" for h in result["hits"])


class ResizedEmbedder(FakeEmbedder):
    """Same model name, different vector size: e.g. a different tag pulled under one name."""

    def _vec(self, text_):
        return super()._vec(text_) + [0.0] * 16


def test_vector_size_change_never_mixes_spaces(login, fake_embedder):
    lena = login("lena.weber@bina.example")
    set_embedder(ResizedEmbedder())
    # RAG-05: the query has 48 dimensions, the index 32. Search degrades instead of failing in SQL.
    result = _search(lena, "thermal validation")
    assert result["mode"] == "keyword" and result["degraded"] == "EMBEDDING_DIMENSION_MISMATCH"
    assert result["hits"][0]["document_title"] == "A17 Thermal Validation Report"
    # ...and the indexer refuses to store 48-dimension vectors next to the 32-dimension ones.
    eng = department_id(lena, "Engineering")
    doc = lena.post(f"/api/departments/{eng}/documents", data={"title": "Rig notes", "classification": "department"},
                    files={"file": ("n.md", b"Rig notes.", "text/markdown")}).json()
    index_all(ResizedEmbedder())
    with owner_conn() as conn:
        row = conn.execute(text("SELECT index_status, index_error_code FROM core.document_versions WHERE document_id = :d"),
                           {"d": doc["id"]}).one()
    assert tuple(row) == ("pending", "EMBEDDING_DIMENSION_MISMATCH")


def test_overlong_embedding_input_is_rejected_not_truncated(monkeypatch):
    from abteilo.ai import embeddings

    monkeypatch.setattr(embeddings.httpx, "post", lambda *a, **k: pytest.fail("must not reach the model"))
    with pytest.raises(EmbeddingError, match="EMBEDDING_INPUT_TOO_LONG"):
        embeddings.OllamaEmbedder("bge-m3").embed_documents(["x" * (embeddings.MAX_INPUT_CHARS + 1)])
