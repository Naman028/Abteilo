"""End-to-end evaluation of combined answers (documents, knowledge graph, live records).

    python -m eval.intelligence --level L1                       # deterministic probes, scripted model (CI)
    python -m eval.intelligence --level L2 --config C2 --runs 3  # real local model, end to end
    python -m eval.intelligence --level L2 --config C2 --stream  # also measures first-visible latency (SSE)
    python -m eval.intelligence --level L2 --cases sealed.json --sealed   # aggregates only

Safety: it resets and seeds ONLY the database named by ABTEILO_TEST_DATABASE_URL /
ABTEILO_TEST_MIGRATION_DATABASE_URL (the disposable test database), never the development database.

Configurations (L2):
- C0  documents only, no sufficiency gate      (what hybrid RAG alone achieves)
- C1  records + documents, no graph, no gate   (approximates the assistant before Block D)
- C2  Block D: records + documents + graph + live re-read + sufficiency gate

Ground truth (intelligence_cases.json) is judged here from the case definitions, not by the code under test.
Without a gate, C0/C1 outcomes are derived from the reply status (verified -> answered, partially_verified ->
partial, insufficient_evidence -> insufficient), as the assistant reported them before Block D.
"""

import argparse
import json
import os
import re
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PASSWORD = "eval-password-123"
CAUSAL = re.compile(r"\b(because|due to|caused by|as a result of|owing to|blocked by|held up by|delayed by|blocking|blocks|holds up|delays|prevents|leading to|results? in|weil|wegen|aufgrund)\b", re.I)
OLD_OUTCOME = {"verified": "answered", "partially_verified": "partial", "insufficient_evidence": "insufficient"}


def _env_from_dotenv() -> None:
    env = HERE.parents[1] / ".env"
    vals = {}
    if env.exists():
        vals = dict(line.split("=", 1) for line in env.read_text(encoding="utf-8").splitlines()
                    if "=" in line and not line.startswith("#"))
    for name in ("ABTEILO_TEST_DATABASE_URL", "ABTEILO_TEST_MIGRATION_DATABASE_URL"):
        if name not in os.environ and name in vals:
            os.environ[name] = vals[name].strip()
    if "ABTEILO_TEST_DATABASE_URL" not in os.environ:
        sys.exit("ABTEILO_TEST_DATABASE_URL is required: the evaluation only runs on the disposable test database.")
    os.environ["ABTEILO_DATABASE_URL"] = os.environ["ABTEILO_TEST_DATABASE_URL"]
    os.environ.setdefault("ABTEILO_ENVIRONMENT", "test")
    os.environ.setdefault("ABTEILO_COOKIE_SECURE", "false")


def _reset_and_seed(level: str, extension: str | None = None) -> None:
    from sqlalchemy import create_engine, text

    from abteilo.config import get_settings
    from abteilo.db import configure_engine, transaction
    from abteilo.indexer import index_all
    from eval.intelligence_seed import build

    owner = create_engine(os.environ["ABTEILO_TEST_MIGRATION_DATABASE_URL"])
    with owner.begin() as conn:
        conn.execute(text("TRUNCATE core.companies, core.users, conv.conversations, core.login_failures CASCADE"))
    owner.dispose()
    get_settings.cache_clear()
    configure_engine(os.environ["ABTEILO_DATABASE_URL"])
    company_id = build(PASSWORD)
    if extension:   # data an independent author declared for the sealed set (eval/sealed/extension.py)
        from abteilo.graph_projection import project_company
        from eval.sealed.extension import apply, load

        apply(company_id, load(extension))
        project_company(company_id)
    if level == "L1":   # route the company to the deterministic scripted model
        with transaction(company_id) as conn:
            conn.execute(text("INSERT INTO core.model_profiles (company_id, code, display_name, provider, model_name)"
                              " VALUES (:c, 'scripted', 'Scripted', 'scripted', 'scripted')"), {"c": company_id})
            conn.execute(text("UPDATE core.model_routes SET profile_code = 'scripted'"))
    elif get_settings().embedding_model:
        index_all()


def _configure(config: str) -> None:
    """C0/C1 restrict the tools and switch the sufficiency gate off; C2 is the system as built."""
    from abteilo.ai import orchestrator

    if config == "C2":
        return
    allowed = {"C0": {"documents.search"}, "C1": None}[config]
    original = orchestrator.available_tools

    def tools(p, ws):
        names = [n for n in original(p, ws) if n != "graph.related"]
        return [n for n in names if allowed is None or n in allowed]

    orchestrator.available_tools = tools
    orchestrator.Prepared.answerable = property(lambda self: True)


def _clients():
    from fastapi.testclient import TestClient

    from abteilo.main import create_app

    app = create_app()
    cache = {}

    def client(user: str):
        if user not in cache:
            c = TestClient(app)
            c.headers["X-Abteilo-CSRF"] = "1"
            r = c.post("/api/auth/login", json={"email": f"{user}@bina.example", "password": PASSWORD})
            r.raise_for_status()
            c.me = r.json()
            cache[user] = c
        return cache[user]
    return client


def _workspace(c, name):
    return None if name is None else next(w["department_id"] for w in c.me["workspaces"] if w["name"] == name)


def _sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        out.append((lines.get("event", ""), json.loads(lines.get("data", "null"))))
    return out


def judge(case: dict, body: dict, config: str) -> dict:
    question = case["question"]
    evidence_text = "\n".join(f"{e['title']}\n{e.get('content') or ''}" for e in body.get("evidence", []))
    generated = json.dumps({k: body.get(k) for k in ("summary", "claims", "follow_up", "missing")}, ensure_ascii=False)
    leaked = sorted({f for f in case.get("forbidden", [])
                     if f in evidence_text or (f in generated and f.lower() not in question.lower())})
    outcome = body.get("outcome") if config == "C2" else OLD_OUTCOME.get(body.get("status"), "insufficient")
    required = case.get("required", [])
    found = [r for r in required if r.lower() in evidence_text.lower()]
    backed_text = "\n".join(c["text"] for c in body.get("claims", []) if c["status"] in ("verified", "cited"))
    stale = sorted({f for f in case.get("not_asserted", []) if f in evidence_text or f in backed_text})
    # A false cause is verified when a verified claim with causal wording names one of the case's false causes
    # (without a list, any verified causal claim counts). A true cause verified from a recorded blocked_by edge
    # in the same answer is not a false cause.
    false_causes = [f.lower() for f in case.get("false_causes", [])]
    false_causal = case.get("no_verified_causal", False) and any(
        c["status"] == "verified" and (CAUSAL.search(c["text"]) or c.get("reason") == "causal_link_recorded")
        and (not false_causes or any(f in c["text"].lower() for f in false_causes))
        for c in body.get("claims", []))
    return {"id": case["id"], "category": case["category"], "outcome": outcome,
            "outcome_ok": outcome in case["expect"], "required_found": len(found), "required_n": len(required),
            "leaked": leaked, "stale_asserted": stale, "false_causal_verified": bool(false_causal),
            "claims": [c["status"] for c in body.get("claims", [])], "tools": body.get("tools_used", []),
            "claim_detail": [{"status": c["status"], "reason": c.get("reason"), "text": c["text"]}
                             for c in body.get("claims", [])]}


def run_l1(cases: list[dict]) -> dict:
    from abteilo.ai.providers import PROVIDERS

    scripted = PROVIDERS["scripted"]
    client = _clients()
    results = []
    for case in [c for c in cases if "probe" in c]:
        probe = case["probe"]
        scripted.replies.clear()
        scripted.replies.extend([json.dumps(probe["plan"]), json.dumps(
            {"summary": "Probe answer.", "summary_evidence_ids": [], "claims": probe["claims"]})])
        c = client(case["user"])
        body = c.post("/api/assistant/messages", json={"department_id": _workspace(c, case["workspace"]),
                                                      "message": case["question"]}).json()
        verdict = judge(case, body, "C2")
        verdict["probe_claims_ok"] = verdict["claims"] == probe["expect_claims"]
        verdict["probe_outcome_ok"] = body.get("outcome") == probe["expect_outcome"]
        results.append(verdict)
    ok = [r for r in results if r["probe_claims_ok"] and r["probe_outcome_ok"] and not r["leaked"]
          and not r["stale_asserted"] and not r["false_causal_verified"]]
    return {"level": "L1", "probes": len(results), "passed": len(ok), "results": results}


def _pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))] if values else None


def run_l2(cases: list[dict], config: str, runs: int, stream: bool) -> dict:
    client = _clients()
    per_run = []
    for run in range(runs):
        results = []
        for case in cases:
            c = client(case["user"])
            body_json = {"department_id": _workspace(c, case["workspace"]), "message": case["question"]}
            start = time.monotonic()
            if stream:
                r = c.post("/api/assistant/messages/stream", json=body_json)
                events = _sse(r.text)
                kind, data = events[-1] if events else ("error", {"code": "NO_EVENTS"})
                body = data if kind == "final" else {"error": data, "claims": [], "evidence": []}
                first = (data.get("timing") or {}).get("first_visible_ms") if kind == "final" else None
            else:
                r = c.post("/api/assistant/messages", json=body_json)
                body = r.json() if r.status_code == 200 else {"error": r.json().get("error"), "claims": [], "evidence": []}
                first = None
            verdict = judge(case, body, config)
            verdict.update(latency_ms=int((time.monotonic() - start) * 1000), first_visible_ms=first,
                           error=(body.get("error") or {}).get("code") if body.get("error") else None)
            results.append(verdict)
        per_run.append(results)
    return {"level": "L2", "config": config, "runs": runs, "stream": stream, "per_run": per_run,
            "summary": summarise(per_run)}


def summarise(per_run: list[list[dict]]) -> dict:
    def rate(results, pred, cats=None):
        sel = [r for r in results if cats is None or r["category"] in cats]
        return round(sum(pred(r) for r in sel) / len(sel), 3) if sel else None

    metrics = {
        "outcome_accuracy": lambda rs: rate(rs, lambda r: r["outcome_ok"]),
        "unanswerable_abstained": lambda rs: rate(rs, lambda r: r["outcome"] == "insufficient", {"unanswerable"}),
        "multihop_required_recall": lambda rs: round(
            sum(r["required_found"] for r in rs if r["category"] == "multihop")
            / max(1, sum(r["required_n"] for r in rs if r["category"] == "multihop")), 3),
        "required_recall_all": lambda rs: round(sum(r["required_found"] for r in rs) / max(1, sum(r["required_n"] for r in rs)), 3),
        "single_source_and_policy_accuracy": lambda rs: rate(rs, lambda r: r["outcome_ok"], {"record", "policy"}),
        "conflict_ok": lambda rs: rate(rs, lambda r: r["outcome_ok"], {"conflict"}),
    }
    out = {name: {"mean": round(statistics.mean(f(rs) or 0 for rs in per_run), 3), "worst": min(f(rs) or 0 for rs in per_run)}
           for name, f in metrics.items()}
    all_results = [r for rs in per_run for r in rs]
    out["unauthorised_disclosures"] = sum(bool(r["leaked"]) for r in all_results)
    out["leaks"] = sorted({(r["id"], tuple(r["leaked"])) for r in all_results if r["leaked"]})
    out["false_causal_verified"] = sum(r["false_causal_verified"] for r in all_results)
    out["stale_asserted"] = sum(bool(r["stale_asserted"]) for r in all_results)
    out["errors"] = sum(bool(r.get("error")) for r in all_results)
    lat = [r["latency_ms"] for r in all_results]
    first = [r["first_visible_ms"] for r in all_results if r.get("first_visible_ms") is not None]
    out["latency_ms"] = {"p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95)}
    if first:
        out["first_visible_ms"] = {"p50": _pct(first, 0.5), "p95": _pct(first, 0.95), "n": len(first)}
    by_cat: dict[str, list[bool]] = {}
    for r in all_results:
        by_cat.setdefault(r["category"], []).append(r["outcome_ok"])
    out["outcome_ok_by_category"] = {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_cat.items())}
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--level", choices=["L1", "L2"], required=True)
    parser.add_argument("--config", choices=["C0", "C1", "C2"], default="C2")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--cases", default=str(HERE / "intelligence_cases.json"))
    parser.add_argument("--sealed", action="store_true", help="print aggregates only (sealed split)")
    parser.add_argument("--seed-extension", help="JSON seed extension of a sealed set (eval/sealed/extension.py)")
    args = parser.parse_args()
    _env_from_dotenv()
    if args.level == "L1":
        os.environ["ABTEILO_EMBEDDING_MODEL"] = ""
    sys.path.insert(0, str(HERE.parent))
    if args.sealed:   # only frozen files run sealed; an edit after freezing is refused
        from eval.sealed.freeze import verify
        verify(args.cases, args.seed_extension)
    elif args.seed_extension:
        raise SystemExit("--seed-extension is for sealed runs only.")
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))["cases"]
    _reset_and_seed(args.level, args.seed_extension)
    if args.level == "L1":
        report = run_l1(cases)
    else:
        _configure(args.config)
        report = run_l2(cases, args.config, args.runs, args.stream)
    report["run_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    name = f"intelligence-{args.level}" + (f"-{args.config}" if args.level == "L2" else "") + ("-stream" if args.stream else "")
    if args.sealed:
        report = {k: v for k, v in report.items() if k in ("level", "config", "runs", "summary", "run_at", "passed", "probes")}
        if "summary" in report:   # which cases leaked, and which strings, would echo gold: counts only
            report["summary"] = {k: v for k, v in report["summary"].items() if k != "leaks"}
        name += "-sealed"
    out = HERE / "results" / f"{name}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    if args.level == "L1":
        print(f"L1 probes passed {report['passed']}/{report['probes']}")
        if not args.sealed:
            for r in report["results"]:
                print(("ok  " if r["probe_claims_ok"] and r["probe_outcome_ok"] and not r["leaked"] and not r["stale_asserted"] else "FAIL"),
                      r["id"], r["outcome"], r["claims"], r["leaked"] or "")
    else:
        print(json.dumps(report["summary"], indent=1, default=str))
        if not args.sealed:
            for r in report["per_run"][0]:
                print(("ok  " if r["outcome_ok"] and not r["leaked"] else "MISS"), r["id"], r["category"], r["outcome"],
                      r["claims"], r["tools"], r["leaked"] or "", r.get("error") or "", r["latency_ms"])
    print("written", out)


if __name__ == "__main__":
    main()
