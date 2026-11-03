"""Knowledge-graph evaluation (GRAPH-09).

    python -m eval.graph            # against the demo company in the configured database

Runs eval/graph_cases.json as the named demo users through the same functions the
API uses. Three groups:
- correct:    the path must be found with exactly the expected edge kinds, or the
              neighbourhood must contain the expected entities;
- false:      no path may be found (the entities are related only through a shared
              person, department or topic);
- restricted: the root or target must be hidden (the same NotFound as a missing
              entity), and listed kinds or entities must never appear.
Restricted results are judged here, from the case definitions, not by the
visibility code under test. Any restricted leak is a release-blocking failure.
"""

import json
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy import text

from abteilo.authz import Principal, load_grants
from abteilo.db import transaction
from abteilo.errors import NotFound
from abteilo.graph import neighbourhood, path

HERE = Path(__file__).resolve().parent

_KEYS = {
    "department": "SELECT id FROM core.departments WHERE name = :k",
    "project": "SELECT id FROM core.projects WHERE code = :k",
    "task": "SELECT id FROM core.tasks WHERE title = :k",
    "purchase_request": "SELECT id FROM core.purchase_requests WHERE title = :k",
    "document": "SELECT id FROM core.documents WHERE title = :k",
    "person": "SELECT u.id FROM core.users u JOIN core.memberships m ON m.user_id = u.id"
              " WHERE m.company_id = core.current_company_id() AND split_part(u.email, '@', 1) = :k",
    "leave_request": "SELECT lr.id FROM core.leave_requests lr JOIN core.users u ON u.id = lr.user_id"
                     " WHERE split_part(u.email, '@', 1) = :k ORDER BY lr.created_at LIMIT 1",
}


def _ref(conn, kind: str, key: str) -> UUID:
    return conn.execute(text(_KEYS[kind]), {"k": key}).scalar_one()


def _entity_id(conn, kind: str, key: str) -> UUID:
    return conn.execute(text("SELECT id FROM core.graph_entities WHERE kind = :k AND ref_id = :r AND quarantine_reason IS NULL"),
                        {"k": kind, "r": _ref(conn, kind, key)}).scalar_one()


def run_case(company_id: UUID, case: dict) -> dict:
    with transaction(company_id) as conn:
        user = _ref(conn, "person", case["user"])
        p = Principal(user, company_id, "", "", load_grants(conn, company_id, user))
        if case["op"] == "path":  # resolved with tenant context only, never through the visibility under test
            src, dst = _entity_id(conn, *case["from"]), _entity_id(conn, *case["to"])
        else:
            root_ref = _ref(conn, *case["root"])
        include = [(k, _ref(conn, k, key)) for k, key in case.get("include", [])]
        exclude = [(k, _ref(conn, k, key)) for k, key in case.get("exclude", [])]
    outcome = {"id": case["id"], "group": case["group"], "ok": False, "leak": False}
    with transaction(company_id, user) as conn:
        try:
            if case["op"] == "path":
                result = path(conn, p, src, dst)
                kinds = [e.kind for e in result.edges]
                outcome["result"] = kinds if result.found else "not_found"
                if case["group"] == "correct":
                    outcome["ok"] = result.found and kinds == case["kinds"]
                elif case["group"] == "false":
                    outcome["ok"] = not result.found
                else:
                    outcome["leak"] = True  # a restricted endpoint was resolved at all
                return outcome
            result = neighbourhood(conn, p, case["root"][0], root_ref, case.get("depth", 2))
            present = {(n.kind, n.ref_id) for n in result.nodes}
            kinds = {n.kind for n in result.nodes}
            leaked = sorted(kinds & set(case.get("exclude_kinds", []))) + [f"{k}:{r}" for k, r in exclude if (k, r) in present]
            hidden_root_expected = case["group"] == "restricted" and not (exclude or case.get("exclude_kinds"))
            outcome["result"] = sorted(kinds)
            outcome["leak"] = bool(leaked) or hidden_root_expected
            outcome["ok"] = all(i in present for i in include) and not outcome["leak"]
            if leaked:
                outcome["leaked"] = leaked
        except NotFound:
            outcome["result"] = "hidden"
            outcome["ok"] = case["group"] == "restricted"
        return outcome


def run_cases(company_id: UUID, cases: list[dict] | None = None) -> dict:
    cases = cases or json.loads((HERE / "graph_cases.json").read_text(encoding="utf-8"))["cases"]
    results = [run_case(company_id, c) for c in cases]
    groups = {g: {"n": sum(r["group"] == g for r in results), "passed": sum(r["ok"] for r in results if r["group"] == g)}
              for g in ("correct", "false", "restricted")}
    return {"groups": groups, "leaks": [r for r in results if r["leak"]],
            "failures": [r for r in results if not r["ok"]], "results": results}


def main() -> None:
    from abteilo.graph_projection import project_company
    from abteilo.seed import SLUG

    with transaction() as conn:
        company_id = conn.execute(text("SELECT m.company_id FROM core.memberships m JOIN core.users u ON u.id = m.user_id"
                                       " WHERE u.email = :e"), {"e": f"owner@{SLUG}.example"}).scalar()
    if company_id is None:
        sys.exit("Seed the demo company first (python -m abteilo.seed).")
    project_company(company_id)
    report = run_cases(company_id)
    for group, v in report["groups"].items():
        print(f"{group:10} {v['passed']}/{v['n']}")
    print("restricted leaks:", len(report["leaks"]))
    for failure in report["failures"]:
        print("FAIL", json.dumps(failure, default=str))
    sys.exit(1 if report["failures"] else 0)


if __name__ == "__main__":
    main()
