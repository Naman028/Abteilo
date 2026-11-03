"""Retrieval evaluation harness (Block B).

    python -m eval.retrieval --model bge-m3 [--model nomic-embed-text]

For each embedding model it creates (or reuses) an isolated evaluation company in
the configured database, loads eval/corpus.json, indexes it with the real local
model, then runs every question as its user in keyword, vector and hybrid mode.

Reported per mode: Recall@5, Recall@10 and MRR@10 at document level, overall and
per question kind; how many no-answer questions correctly return nothing; query-embedding and search latency; and authorisation results.
Authorisation is judged against ground truth computed from the corpus metadata,
independently of the application's own visibility filter. Any unauthorised hit
is a release-blocking failure.
"""

import argparse
import json
import secrets
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import text

from abteilo import company
from abteilo.ai.embeddings import OllamaEmbedder, set_embedder
from abteilo.authz import Principal, load_grants
from abteilo.config import get_settings
from abteilo.db import transaction
from abteilo.indexer import index_company
from abteilo.modules.documents import hybrid_search, store_document
from abteilo.templates import MAJOR_DEPARTMENTS

HERE = Path(__file__).resolve().parent
MODES = ("keyword", "vector", "hybrid")
CONFIDENTIAL_ROLES = {"hr_specialist", "finance_analyst", "department_lead"}
THRESHOLDS = (0.45, 0.55, 0.65, 0.75, 2.0)  # 2.0 = no distance cut-off


def _slug(model: str) -> str:
    return "eval-" + "".join(c if c.isalnum() else "-" for c in model.lower()).strip("-")[:40]


def _principal(company_id: UUID, user_id: UUID) -> Principal:
    with transaction(company_id, user_id) as conn:
        return Principal(user_id, company_id, "", "", load_grants(conn, company_id, user_id))


def build_company(corpus: dict, model: str) -> tuple[UUID, dict[str, UUID]]:
    """Idempotent: reuses the evaluation company of this model if it already exists."""
    slug = _slug(model)
    owner_email = f"owner@{slug}.example"
    with transaction() as conn:
        existing = conn.execute(text(
            "SELECT m.company_id FROM core.users u JOIN core.memberships m ON m.user_id = u.id WHERE u.email = :e"
        ), {"e": owner_email}).scalar()
    if existing is None:
        created = company.create_company(
            name=f"Eval Corp ({model})", slug=slug, industry="manufacturing", owner_email=owner_email,
            owner_name="Eval Owner", owner_password=secrets.token_urlsafe(24), department_templates=list(MAJOR_DEPARTMENTS))
        company_id, depts = created.company_id, created.department_ids
        with transaction(company_id, created.owner_user_id) as conn:
            people = {**corpus["users"], **{f"editor_{d}": {"department": d, "role": "department_lead"} for d in depts}}
            for key, spec in people.items():
                uid = company.add_member(conn, company_id, created.owner_user_id, email=f"{key}@{slug}.example",
                                         display_name=key, password=secrets.token_urlsafe(24), title=None)
                company.assign_role(conn, company_id, uid, spec["role"], depts[spec["department"]], created.owner_user_id)
    else:
        company_id = existing
    with transaction(company_id) as conn:
        users = dict(conn.execute(text(
            "SELECT split_part(u.email, '@', 1), u.id FROM core.users u JOIN core.memberships m ON m.user_id = u.id"
            " WHERE m.company_id = :c"), {"c": company_id}).all())
        depts = dict(conn.execute(text("SELECT code, id FROM core.departments")).all())
        loaded = conn.execute(text("SELECT count(*) FROM core.documents")).scalar_one()
    if loaded == 0:
        for doc in corpus["documents"]:
            editor = _principal(company_id, users[f"editor_{doc['department']}"])
            with transaction(company_id, editor.user_id) as conn:
                store_document(conn, editor, depts[doc["department"]], doc["title"], doc["classification"], doc["content"])
    return company_id, users


def visible(doc: dict, user: dict) -> bool:
    """Ground truth from corpus metadata, independent of the application's SQL."""
    if doc["classification"] == "company":
        return True
    if doc["department"] != user["department"]:
        return False
    return doc["classification"] == "department" or user["role"] in CONFIDENTIAL_ROLES


def _doc_ranking(hits, title_to_key: dict[str, str]) -> list[str]:
    ranked: list[str] = []
    for h in hits:
        key = title_to_key[h.document_title]
        if key not in ranked:
            ranked.append(key)
    return ranked


def evaluate(model: str, corpus: dict) -> dict:
    settings = get_settings()
    embedder = OllamaEmbedder(model)
    set_embedder(embedder)
    embedder.embed_query("warm up")  # load the model before timing anything
    company_id, users = build_company(corpus, model)

    started = time.monotonic()
    while index_company(company_id, embedder, limit=50).indexed:
        pass
    with transaction(company_id) as conn:
        status = dict(conn.execute(text("SELECT index_status, count(*) FROM core.document_versions GROUP BY 1")).all())
    index_seconds = time.monotonic() - started

    docs = {d["key"]: d for d in corpus["documents"]}
    title_to_key = {d["title"]: d["key"] for d in corpus["documents"]}
    principals = {key: _principal(company_id, users[key]) for key in corpus["users"]}
    query_vectors, embed_ms = {}, []
    for item in corpus["questions"] + corpus.get("holdout", []) + corpus["security"] + corpus.get("no_answer", []):
        t = time.monotonic()
        query_vectors[item["id"]] = embedder.embed_query(item["q"])
        embed_ms.append((time.monotonic() - t) * 1000)

    def search(mode: str, item: dict):
        p = principals[item["user"]]
        with transaction(company_id, p.user_id) as conn:
            t = time.monotonic()
            result = hybrid_search(conn, p, item["q"], None, limit=10, mode=mode, query_vector=query_vectors[item["id"]])
            elapsed = (time.monotonic() - t) * 1000
        assert result.degraded is None, result.degraded
        return result, elapsed

    def run(mode: str, item: dict) -> tuple[list[str], float]:
        result, elapsed = search(mode, item)
        return _doc_ranking(result.hits, title_to_key), elapsed

    report: dict = {"model": model, "dimensions": len(next(iter(query_vectors.values()))),
                    "index": {"seconds": round(index_seconds, 1), "status": status},
                    "embed_query_ms": {"p50": round(statistics.median(embed_ms)), "p95": round(sorted(embed_ms)[int(0.95 * len(embed_ms)) - 1])},
                    "sweep": {}, "unauthorised_hits": 0, "security_violations": []}
    default_threshold = settings.vector_max_distance
    for threshold in THRESHOLDS:
        settings.vector_max_distance = threshold
        per_mode = {}
        for mode in MODES if threshold == default_threshold else ("vector", "hybrid"):
            rows, holdout_rows, search_ms = [], [], []
            for q in corpus["questions"] + corpus.get("holdout", []):
                ranking, ms = run(mode, q)
                search_ms.append(ms)
                user = corpus["users"][q["user"]]
                report["unauthorised_hits"] += sum(not visible(docs[k], user) for k in ranking)
                relevant = set(q["relevant"])
                first = next((i + 1 for i, k in enumerate(ranking) if k in relevant), None)
                (holdout_rows if q["id"].startswith("ho-") else rows).append(
                    {"id": q["id"], "kind": q["kind"], "r5": len(relevant & set(ranking[:5])) / len(relevant),
                     "r10": len(relevant & set(ranking[:10])) / len(relevant), "rr": 1 / first if first else 0.0,
                     "top": ranking[:3]})
            # No-answer questions: nothing in the corpus answers them, so the right result is no hit at all.
            answered = []
            for na in corpus.get("no_answer", []):
                result, _ = search(mode, na)
                if result.hits:
                    answered.append({"id": na["id"], "top": title_to_key[result.hits[0].document_title],
                                     "distance": result.hits[0].distance, "match": result.hits[0].match})
            no_answer = {"n": len(corpus.get("no_answer", [])), "abstained": len(corpus.get("no_answer", [])) - len(answered),
                         "answered": answered}
            for s in corpus["security"]:
                ranking, _ = run(mode, s)
                leaked = sorted(set(ranking) & set(s["forbidden"]))
                if leaked:
                    report["security_violations"].append({"id": s["id"], "mode": mode, "threshold": threshold, "leaked": leaked})

            def mean(key, subset):
                return round(sum(r[key] for r in subset) / len(subset), 3) if subset else None

            kinds = sorted({r["kind"] for r in rows})
            per_mode[mode] = {
                "recall@5": mean("r5", rows), "recall@10": mean("r10", rows), "mrr@10": mean("rr", rows),
                "by_kind": {k: {"recall@5": mean("r5", [r for r in rows if r["kind"] == k]),
                                "mrr@10": mean("rr", [r for r in rows if r["kind"] == k]),
                                "n": sum(r["kind"] == k for r in rows)} for k in kinds},
                "search_ms": {"p50": round(statistics.median(search_ms), 1),
                              "p95": round(sorted(search_ms)[int(0.95 * len(search_ms)) - 1], 1)},
                "misses": [r["id"] for r in rows if r["r10"] < 1.0],
                "no_answer": no_answer,
                "holdout": {"recall@5": mean("r5", holdout_rows), "recall@10": mean("r10", holdout_rows),
                            "mrr@10": mean("rr", holdout_rows), "n": len(holdout_rows),
                            "misses": [r["id"] for r in holdout_rows if r["r10"] < 1.0]},
            }
        report["sweep"][str(threshold)] = per_mode
    settings.vector_max_distance = default_threshold
    report["default_threshold"] = default_threshold
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", action="append", required=True)
    args = parser.parse_args()
    corpus = json.loads((HERE / "corpus.json").read_text(encoding="utf-8"))
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    for model in args.model:
        report = evaluate(model, corpus)
        report["run_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        (out_dir / f"{_slug(model)}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        d = report["sweep"][str(report["default_threshold"])]
        print(f"\n== {model} ({report['dimensions']} dims) index {report['index']} "
              f"embed p50 {report['embed_query_ms']['p50']} ms ==")
        for mode in MODES:
            m = d[mode]
            h = m["holdout"]
            print(f"{mode:8} R@5 {m['recall@5']:.3f}  R@10 {m['recall@10']:.3f}  MRR {m['mrr@10']:.3f}  "
                  f"search p50 {m['search_ms']['p50']} ms  misses {m['misses']}  | holdout R@5 {h['recall@5']:.3f} "
                  f"MRR {h['mrr@10']:.3f} misses {h['misses']} | no-answer abstained "
                  f"{m['no_answer']['abstained']}/{m['no_answer']['n']}")
        for threshold, per_mode in report["sweep"].items():
            print(f"threshold {threshold}: " + ", ".join(
                f"{mode} R@5 {m['recall@5']:.3f} abstain {m['no_answer']['abstained']}/{m['no_answer']['n']}"
                for mode, m in per_mode.items()))
        print("unauthorised hits:", report["unauthorised_hits"], "| security violations:", report["security_violations"])


if __name__ == "__main__":
    main()
