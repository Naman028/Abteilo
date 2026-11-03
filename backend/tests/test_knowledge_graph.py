"""Block C: knowledge-graph invariants (GRAPH-01..10) against real PostgreSQL."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from abteilo import company, graph, graph_projection
from abteilo.ai.providers import PROVIDERS
from abteilo.db import transaction
from abteilo.graph_extraction import extract_company
from abteilo.graph_projection import project_company
from abteilo.templates import MAJOR_DEPARTMENTS
from eval.graph import run_cases
from tests.conftest import PASSWORD, _alembic, department_id, owner_conn, user_id


def _entity(kind: str, where: str, **params):
    with owner_conn() as conn:
        return conn.execute(text(f"SELECT e.id, e.ref_id FROM core.graph_entities e WHERE e.kind = :k AND e.ref_id = ({where})"),
                            {"k": kind, **params}).one()


def _project(code: str):
    return _entity("project", "SELECT id FROM core.projects WHERE code = :c", c=code)


def _task(title: str):
    return _entity("task", "SELECT id FROM core.tasks WHERE title = :t", t=title)


def _hood(client, kind, ref_id, depth=2):
    return client.get(f"/api/graph/{kind}/{ref_id}/neighbourhood", params={"depth": depth})


@pytest.fixture()
def projected(demo):
    project_company(demo)
    return demo


def test_projection_is_idempotent_evidence_backed_and_append_only(projected, demo):
    assert project_company(demo).changes == 0                       # GRAPH-04: a second run changes nothing
    with owner_conn() as conn:
        missing = conn.execute(text(
            "SELECT count(*) FROM core.graph_relationships r WHERE NOT EXISTS"
            " (SELECT 1 FROM core.graph_evidence e WHERE e.relationship_id = r.id)")).scalar_one()
        assert missing == 0                                          # GRAPH-03
        kinds = dict(conn.execute(text("SELECT kind, count(*) FROM core.graph_entities GROUP BY 1")).all())
    assert kinds["project"] == 3 and kinds["task"] == 5 and kinds["document"] == 7
    a17 = _project("A17")
    with pytest.raises(IntegrityError, match="without evidence"):    # the database refuses evidence-free edges
        with transaction(demo) as conn:
            conn.execute(text("INSERT INTO core.graph_relationships (id, company_id, src_id, dst_id, kind, authority)"
                              " SELECT :id, :c, :s, id, 'depends_on', 'record' FROM core.graph_entities"
                              " WHERE kind = 'project' AND id <> :s LIMIT 1"), {"id": uuid4(), "c": demo, "s": a17.id})
    for statement in ("DELETE FROM core.graph_relationships", "DELETE FROM core.graph_entities",
                      "UPDATE core.graph_relationships SET kind = 'blocked_by'", "UPDATE core.graph_evidence SET quote = 'x'"):
        with pytest.raises(ProgrammingError, match="permission denied"):
            with transaction(demo) as conn:
                conn.execute(text(statement))


def test_engineer_finds_dependencies_but_no_finance_or_hr_facts(login, projected):
    jonas, lena, sara, owner = (login(f"{n}@bina.example") for n in ("jonas.becker", "lena.weber", "sara.koch", "owner"))
    a17 = _project("A17")
    view = _hood(jonas, "project", a17.ref_id).json()
    labels = {(n["kind"], n["label"]) for n in view["nodes"]}
    assert ("task", "Thermal validation retest") in labels and ("document", "A17 Thermal Validation Report") in labels
    assert ("purchase_request", "Thermal test rig") in labels            # his own request
    assert not {k for k, _ in labels} & {"leave_request", "access_request"}
    mention = next(e for e in view["edges"] if e["kind"] == "mentions")
    assert mention["authority"] == "document_mention"
    assert {e["evidence"][0]["quote"] for e in view["edges"] if e["kind"] == "mentions"} == {
        "Project A17 thermal validation, revision 3.",                      # whole sentence, nothing clipped
        "Every engineering project has a code such as A17 and an owning lead."}
    # GRAPH-02/08: Lena leads the project but may not read finance records; the path is cut there.
    assert all(n["kind"] != "purchase_request" for n in _hood(lena, "project", a17.ref_id).json()["nodes"])
    # Finance sees the request but not the engineering project it is for; the owner sees no project at all.
    hidden = _hood(sara, "project", a17.ref_id)
    assert hidden.status_code == 404
    assert _hood(sara, "project", uuid4()).json()["error"]["code"] == hidden.json()["error"]["code"]  # no oracle
    assert _hood(owner, "project", a17.ref_id).status_code == 404
    with owner_conn() as conn:
        rig = conn.execute(text("SELECT id FROM core.purchase_requests WHERE title = 'Thermal test rig'")).scalar_one()
    kinds = {n["kind"] for n in _hood(sara, "purchase_request", rig).json()["nodes"]}
    assert "purchase_request" in kinds and not kinds & {"project", "task"}


def test_changed_and_archived_records_end_outdated_edges(login, projected, demo):
    lena = login("lena.weber@bina.example")
    retest = _task("Thermal validation retest")
    lena_id = user_id("lena.weber@bina.example")
    assert lena.patch(f"/api/engineering/tasks/{retest.ref_id}", json={"assignee_id": str(lena_id)}).status_code == 200
    with owner_conn() as conn:
        report = conn.execute(text("SELECT id FROM core.documents WHERE title = 'A17 Thermal Validation Report'")).scalar_one()
    lena.post(f"/api/documents/{report}/versions",
              files={"file": ("r.md", b"Thermal validation, revision 4. No project code here.", "text/markdown")})
    run = project_company(demo)
    assert run.edges_ended == 2 and run.edges_added == 1               # old assignee, superseded mention; new assignee
    with owner_conn() as conn:
        ended = dict(conn.execute(text(
            "SELECT kind, ended_reason FROM core.graph_relationships WHERE valid_to IS NOT NULL")).all())
        current = conn.execute(text(
            "SELECT p.email FROM core.graph_relationships r JOIN core.graph_entities e ON e.id = r.dst_id"
            " JOIN core.users p ON p.id = e.ref_id WHERE r.src_id = :t AND r.kind = 'assigned_to' AND r.valid_to IS NULL"),
            {"t": retest.id}).scalar_one()
    assert ended == {"assigned_to": "source_changed", "mentions": "document_superseded"}  # GRAPH-05
    assert current == "lena.weber@bina.example"
    lena.post(f"/api/documents/{report}/archive")
    project_company(demo)
    with owner_conn() as conn:
        assert conn.execute(text("SELECT ended_reason FROM core.graph_relationships r JOIN core.graph_entities e"
                                 " ON e.id = r.dst_id WHERE e.ref_id = :d AND r.valid_to IS NOT NULL"),
                            {"d": report}).scalar_one() == "source_archived"
    with pytest.raises(IntegrityError, match="already ended"):         # history is never reopened
        with transaction(demo) as conn:
            conn.execute(text("UPDATE core.graph_relationships SET valid_to = NULL, ended_reason = NULL"
                              " WHERE valid_to IS NOT NULL"))


def _queue_extraction(*relationships):
    PROVIDERS["scripted"].replies.append(json.dumps({"relationships": list(relationships)}))


QUOTE = "A replacement cooling module and a new test rig are required before the retest."


def test_suggestions_never_become_edges_without_review(login, projected, demo):
    with owner_conn() as conn:  # the report mentions A17; entities listed: A17, its 3 tasks, its purchase request
        conn.execute(text("INSERT INTO core.graph_extraction_runs (document_version_id, company_id, status)"
                          " SELECT v.id, v.company_id, 'done' FROM core.document_versions v JOIN core.documents d"
                          " ON d.id = v.document_id WHERE d.title <> 'A17 Thermal Validation Report'"))
    # E1 = A17, E2 = the purchase request, E3 = "Order replacement cooling module", E4 = "Thermal validation retest".
    _queue_extraction(
        {"source": "E4", "target": "E3", "kind": "depends_on", "quote": QUOTE},          # valid
        {"source": "E1", "target": "E2", "kind": "blocked_by", "quote": QUOTE},          # valid, finance entity
        {"source": "E4", "target": "E1", "kind": "delays", "quote": "A17 is cancelled."},  # not in the document
        {"source": "E9", "target": "E1", "kind": "affects", "quote": QUOTE},             # unknown entity
    )
    run = extract_company(demo)
    assert (run.done, run.candidates) == (1, 2)
    with owner_conn() as conn:
        assert conn.execute(text("SELECT count(*) FROM core.graph_relationships WHERE authority = 'reviewed'")).scalar_one() == 0
        assert conn.execute(text("SELECT stage FROM core.ai_runs ORDER BY created_at DESC LIMIT 1")).scalar_one() == "extract"
    lena, jonas = login("lena.weber@bina.example"), login("jonas.becker@bina.example")
    retest = _task("Thermal validation retest")
    hood = _hood(lena, "task", retest.ref_id).json()
    assert all(e["kind"] != "depends_on" for e in hood["edges"])          # GRAPH-06: candidates are not traversed
    eng = department_id(lena, "Engineering")
    assert jonas.get(f"/api/departments/{eng}/graph/candidates").status_code == 403  # reviewers edit documents
    queue = lena.get(f"/api/departments/{eng}/graph/candidates").json()
    assert [c["kind"] for c in queue] == ["depends_on"]                    # the finance one is not shown to her
    decided = lena.post(f"/api/graph/candidates/{queue[0]['id']}/decision", json={"decision": "accept"})
    assert decided.status_code == 200 and decided.json()["status"] == "accepted"
    assert lena.post(f"/api/graph/candidates/{queue[0]['id']}/decision", json={"decision": "reject"}).status_code == 409
    edge = next(e for e in _hood(lena, "task", retest.ref_id).json()["edges"] if e["kind"] == "depends_on")
    assert edge["authority"] == "reviewed" and edge["evidence"][0]["quote"] == QUOTE
    # A new document version ends the reviewed edge and supersedes the open suggestion.
    with owner_conn() as conn:
        report = conn.execute(text("SELECT id FROM core.documents WHERE title = 'A17 Thermal Validation Report'")).scalar_one()
    lena.post(f"/api/documents/{report}/versions", files={"file": ("r.md", b"Project A17 retest passed.", "text/markdown")})
    project_company(demo)
    with owner_conn() as conn:
        assert conn.execute(text("SELECT ended_reason FROM core.graph_relationships WHERE authority = 'reviewed'")).scalar_one() \
            == "document_superseded"
        assert dict(conn.execute(text("SELECT status, count(*) FROM core.graph_candidates GROUP BY 1")).all()) \
            == {"accepted": 1, "superseded": 1}
        assert conn.execute(text("SELECT count(*) FROM core.audit_events WHERE action = 'graph.relationship_accepted'")).scalar_one() == 1


def test_traversal_is_bounded_and_hubs_do_not_connect_unrelated_work(login, projected, monkeypatch):
    jonas = login("jonas.becker@bina.example")
    a17, b24 = _project("A17"), _project("B24")
    # Both projects have tasks assigned to Jonas; a shared person is not a relationship.
    assert jonas.get("/api/graph/path", params={"source": a17.id, "target": b24.id}).json()["found"] is False
    assert _hood(jonas, "project", a17.ref_id, depth=4).status_code == 422
    monkeypatch.setattr(graph, "MAX_NODES", 3)
    view = _hood(jonas, "project", a17.ref_id, depth=3).json()
    assert view["truncated"] is True and len(view["nodes"]) == 3
    monkeypatch.setattr(graph, "STATEMENT_TIMEOUT", "50ms")
    original = graph._expand

    def slow(conn, access, frontier):
        conn.execute(text("SELECT pg_sleep(1)"))
        return original(conn, access, frontier)

    monkeypatch.setattr(graph, "_expand", slow)
    timed_out = _hood(jonas, "project", a17.ref_id)
    assert timed_out.status_code == 503 and timed_out.json()["error"]["code"] == "GRAPH_TIMEOUT"


def test_projection_failure_rolls_back_and_never_widens_access(login, demo, monkeypatch):
    jonas = login("jonas.becker@bina.example")
    with owner_conn() as conn:
        before = conn.execute(text("SELECT (SELECT count(*) FROM core.tasks), (SELECT count(*) FROM core.projects)")).one()
        a17 = conn.execute(text("SELECT id FROM core.projects WHERE code = 'A17'")).scalar_one()

    def broken(conn):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(graph_projection, "_sync_edges", broken)
    with pytest.raises(RuntimeError):
        project_company(demo)
    with owner_conn() as conn:                                          # GRAPH-10: all or nothing
        assert conn.execute(text("SELECT count(*) FROM core.graph_entities")).scalar_one() == 0
        assert tuple(conn.execute(text("SELECT (SELECT count(*) FROM core.tasks), (SELECT count(*) FROM core.projects)")).one()) \
            == tuple(before)
        assert conn.execute(text("SELECT last_status, last_error_code FROM core.graph_projection_state")).one() \
            == ("failed", "GRAPH_PROJECTION_FAILED")
    assert _hood(jonas, "project", a17).status_code == 404               # no graph yet: nothing, not more
    owner = login("owner@bina.example")
    status = owner.get("/api/admin/graph").json()
    assert status["last_status"] == "failed" and status["entities"] == {}
    assert jonas.get("/api/admin/graph").status_code == 403


def _second_company(shared: bool = True) -> dict:
    """Nordwerk: its own people and work, plus (if shared) Jonas from BINA as a legitimate member of both."""
    created = company.create_company(
        name="Nordwerk AG (test)", slug="nordwerk", industry="manufacturing", owner_email="owner@nordwerk.example",
        owner_name="Nord Owner", owner_password=PASSWORD, department_templates=list(MAJOR_DEPARTMENTS))
    cid, owner, eng = created.company_id, created.owner_user_id, created.department_ids["engineering"]
    jonas = user_id("jonas.becker@bina.example")
    with transaction(cid, owner) as conn:
        kai = company.add_member(conn, cid, owner, email="kai.vogt@nordwerk.example", display_name="Kai Vogt",
                                 password=PASSWORD, title="Test Engineer")
        if shared:
            conn.execute(text("INSERT INTO core.memberships (company_id, user_id, title) VALUES (:c, :u, 'Consultant')"),
                         {"c": cid, "u": jonas})
            company.assign_role(conn, cid, jonas, "employee", None, owner)
        project = uuid4()
        conn.execute(text("INSERT INTO core.projects (id, company_id, department_id, code, name, status, created_by)"
                          " VALUES (:id, :c, :d, 'N01', 'Gearbox Endurance', 'active', :u)"),
                     {"id": project, "c": cid, "d": eng, "u": owner})
        for title, assignee in (("Gearbox endurance run", jonas if shared else kai), ("Line audit", kai)):
            conn.execute(text("INSERT INTO core.tasks (id, company_id, project_id, title, status, assignee_id, created_by)"
                              " VALUES (:id, :c, :p, :t, 'todo', :a, :u)"),
                         {"id": uuid4(), "c": cid, "p": project, "t": title, "a": assignee, "u": owner})
    return {"id": cid, "kai": kai, "jonas": jonas, "project": project}


def _graph(company_id) -> dict:
    """Per-company view through the owner role, under that company's tenant context."""
    with owner_conn() as conn:
        conn.execute(text("SELECT set_config('app.company_id', :c, true)"), {"c": str(company_id)})
        members = set(conn.execute(text("SELECT user_id FROM core.memberships WHERE company_id = :c"),
                                   {"c": company_id}).scalars())
        people = dict(conn.execute(text("SELECT ref_id, id FROM core.graph_entities WHERE kind = 'person' AND active")).all())
        refs = set(conn.execute(text("SELECT ref_id FROM core.graph_entities")).scalars())
        assigned = conn.execute(text(
            "SELECT t.ref_id FROM core.graph_relationships r JOIN core.graph_entities t ON t.id = r.src_id"
            " JOIN core.graph_entities p ON p.id = r.dst_id"
            " WHERE r.kind = 'assigned_to' AND r.valid_to IS NULL AND p.ref_id = :j"),
            {"j": user_id("jonas.becker@bina.example")}).scalars().all()
    return {"members": members, "people": people, "refs": refs, "jonas_tasks": set(assigned)}


def test_projection_stays_inside_the_company(login, projected, demo):
    nord = _second_company()
    for _ in range(2):                                                   # GRAPH-04 across tenants: rebuilds are stable
        project_company(nord["id"])
    assert project_company(demo).changes == 0 and project_company(nord["id"]).changes == 0
    bina, other = _graph(demo), _graph(nord["id"])
    # GRAPH-11: the people in a company's graph are exactly that company's members.
    assert set(bina["people"]) == bina["members"] and len(bina["members"]) == 7
    assert set(other["people"]) == other["members"] == {nord["jonas"], nord["kai"], user_id("owner@nordwerk.example")}
    # The shared person exists once per company, as two separate entities; nothing else is shared.
    assert bina["refs"] & other["refs"] == {nord["jonas"]}
    assert bina["people"][nord["jonas"]] != other["people"][nord["jonas"]]
    # His Nordwerk task does not reach BINA's graph, nor his BINA tasks Nordwerk's.
    assert len(bina["jonas_tasks"]) == 3 and len(other["jonas_tasks"]) == 1
    assert not bina["jonas_tasks"] & other["jonas_tasks"]
    jonas, owner = login("jonas.becker@bina.example"), login("owner@bina.example")   # both sessions are BINA's
    assert _hood(jonas, "project", nord["project"]).status_code == 404
    assert _hood(jonas, "person", nord["kai"]).status_code == 404
    labels = {n["label"] for n in _hood(jonas, "person", nord["jonas"]).json()["nodes"]}
    assert "Thermal validation retest" in labels and not labels & {"Gearbox endurance run", "Line audit", "N01 Gearbox Endurance"}
    assert owner.get("/api/admin/graph").json()["entities"]["person"] == 7


def test_database_refuses_foreign_people_whatever_the_projection_does(projected, demo, monkeypatch):
    nord = _second_company(shared=False)
    # The defect itself: the person query without its company filter. The database rejects the run as a whole.
    unscoped = graph_projection._ENTITY_SQL["person"].split(" WHERE ")[0]
    monkeypatch.setitem(graph_projection._ENTITY_SQL, "person", unscoped)
    with pytest.raises(IntegrityError, match="graph_entities_person_membership_fk"):
        project_company(demo)
    assert set(_graph(demo)["people"]) == _graph(demo)["members"]        # the previous graph stands
    with pytest.raises(IntegrityError, match="graph_entities_person_membership_fk"):
        with transaction(demo) as conn:                                   # nor can any direct write place one
            conn.execute(text(
                "INSERT INTO core.graph_entities (id, company_id, kind, ref_id, label, vis_scope, active)"
                " VALUES (:id, :c, 'person', :u, 'Kai Vogt', 'company', false)"), {"id": uuid4(), "c": demo, "u": nord["kai"]})


@pytest.fixture()
def contaminated(demo):
    """Schema 0005 with the defect's footprint in BINA's graph: a Nordwerk-only person and an edge to them."""
    project_company(demo)
    nord = _second_company()
    _alembic("downgrade", "0005")
    try:
        with owner_conn() as conn:
            kai_entity, edge = uuid4(), uuid4()
            conn.execute(text("INSERT INTO core.graph_entities (id, company_id, kind, ref_id, label, vis_scope, active)"
                              " VALUES (:id, :c, 'person', :u, 'Kai Vogt', 'company', true)"),
                         {"id": kai_entity, "c": demo, "u": nord["kai"]})
            task = conn.execute(text("SELECT id, ref_id FROM core.graph_entities WHERE kind = 'task' LIMIT 1")).one()
            conn.execute(text("INSERT INTO core.graph_relationships (id, company_id, src_id, dst_id, kind, authority)"
                              " VALUES (:id, :c, :s, :d, 'assigned_to', 'record')"),
                         {"id": edge, "c": demo, "s": task.id, "d": kai_entity})
            conn.execute(text("INSERT INTO core.graph_evidence (id, company_id, relationship_id, source_type, source_id)"
                              " VALUES (:id, :c, :r, 'task', :t)"), {"id": uuid4(), "c": demo, "r": edge, "t": task.ref_id})
        _alembic("upgrade")
        yield {**nord, "entity": kai_entity, "edge": edge}
    finally:
        _alembic("upgrade")
        with owner_conn() as conn:  # quarantined rows block a downgrade by design; leave the next session a clean slate
            conn.execute(text("TRUNCATE core.companies, core.users, conv.conversations, core.login_failures CASCADE"))


def test_migration_quarantines_existing_foreign_people(login, contaminated, demo):
    with owner_conn() as conn:
        row = conn.execute(text("SELECT active, label, quarantine_reason, person_user_id FROM core.graph_entities"
                                " WHERE id = :id"), {"id": contaminated["entity"]}).one()
        edge = conn.execute(text("SELECT ended_reason FROM core.graph_relationships WHERE id = :id"),
                            {"id": contaminated["edge"]}).scalar_one()
        audit = conn.execute(text("SELECT details FROM core.audit_events WHERE action = 'graph.entities_quarantined'")).scalar_one()
    assert tuple(row) == (False, "Quarantined person", "foreign_membership", None)  # kept, not deleted; name removed
    assert edge == "source_archived"
    assert audit == {"migration": "0006", "reason": "foreign_membership", "entities": 1,
                     "relationships_ended": 1, "candidates_superseded": 0}
    owner = login("owner@bina.example")
    assert owner.get("/api/admin/graph").json()["entities"]["person"] == 7
    assert _hood(owner, "person", contaminated["kai"]).status_code == 404
    with pytest.raises(IntegrityError, match="violates check constraint"):     # never active again
        with transaction(demo) as conn:
            conn.execute(text("UPDATE core.graph_entities SET active = true WHERE quarantine_reason IS NOT NULL"))
    with pytest.raises(ProgrammingError, match="permission denied"):                 # nor released by the app
        with transaction(demo) as conn:
            conn.execute(text("UPDATE core.graph_entities SET quarantine_reason = NULL, quarantined_at = NULL"))
    # If the person later joins BINA for real, they get a new, validated entity; the quarantined row stays as it was.
    with owner_conn() as conn:
        conn.execute(text("INSERT INTO core.memberships (company_id, user_id) VALUES (:c, :u)"), {"c": demo, "u": contaminated["kai"]})
    project_company(demo)
    assert project_company(demo).changes == 0
    people = _graph(demo)["people"]
    assert contaminated["kai"] in people and people[contaminated["kai"]] != contaminated["entity"]
    with pytest.raises(Exception, match="graph_entities_downgrade_requires_no_quarantine"):
        _alembic("downgrade", "0005")                                     # rollback would erase the quarantine record


def test_suspended_person_disappears_from_the_graph_at_once(login, projected, demo):
    """INT-08: graph visibility of a person follows live membership. No projection run is needed."""
    owner, jonas = login("owner@bina.example"), login("jonas.becker@bina.example")
    lena = user_id("lena.weber@bina.example")
    a17 = _project("A17")
    def people():
        return {n["label"] for n in _hood(jonas, "project", a17.ref_id, depth=2).json()["nodes"] if n["kind"] == "person"}
    assert "Lena Weber" in people() and _hood(jonas, "person", lena).status_code == 200
    assert owner.patch(f"/api/admin/members/{lena}", json={"status": "disabled"}).status_code == 200
    assert _hood(jonas, "person", lena).status_code == 404                  # hidden immediately
    assert "Lena Weber" not in people()                                       # and every edge to her with it
    with owner_conn() as conn:                                                # stored state follows in the same transaction
        assert conn.execute(text("SELECT active FROM core.graph_entities WHERE kind = 'person' AND ref_id = :u"),
                            {"u": lena}).scalar_one() is False
    run = project_company(demo)                  # the projection agrees on the entity and ends her edges (history kept)
    assert (run.entities_changed, run.edges_added) == (0, 0) and run.edges_ended >= 1
    assert owner.patch(f"/api/admin/members/{lena}", json={"status": "active"}).status_code == 200
    assert _hood(jonas, "person", lena).status_code == 200                    # visible again at once
    project_company(demo)                                                     # her edges are re-created by the next run
    assert "Lena Weber" in people()


def test_graph_reads_are_logged_as_metadata_and_kept_ninety_days(login, projected, demo, monkeypatch):
    """INT-09: every graph read (ok, hidden or timed out) is logged without content; only audit.read exports it;
    the application can neither change nor delete young rows."""
    from datetime import UTC, datetime, timedelta

    from abteilo.retention import purge_company

    jonas, owner = login("jonas.becker@bina.example"), login("owner@bina.example")
    a17, b24 = _project("A17"), _project("B24")
    assert _hood(jonas, "project", a17.ref_id).status_code == 200
    assert _hood(owner, "project", a17.ref_id).status_code == 404                 # hidden for the owner
    assert jonas.get("/api/graph/path", params={"source": a17.id, "target": b24.id}).status_code == 200
    monkeypatch.setattr(graph, "STATEMENT_TIMEOUT", "50ms")
    original = graph._expand
    monkeypatch.setattr(graph, "_expand", lambda c, a, f: (c.execute(text("SELECT pg_sleep(1)")), original(c, a, f))[1])
    assert _hood(jonas, "project", a17.ref_id).status_code == 503
    window = {"since": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(), "until": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}
    assert jonas.get("/api/admin/graph-read-log", params=window).status_code == 403
    rows = owner.get("/api/admin/graph-read-log", params=window).json()
    assert [(r["actor_name"], r["operation"], r["outcome"]) for r in rows] == [
        ("Jonas Becker", "neighbourhood", "ok"), ("Anna Bauer", "neighbourhood", "not_found"),
        ("Jonas Becker", "path", "ok"), ("Jonas Becker", "neighbourhood", "timeout")]
    assert rows[0]["root_ref_id"] == str(a17.ref_id) and rows[0]["node_count"] > 1 and "label" not in rows[0]
    with owner_conn() as conn:
        assert conn.execute(text("SELECT count(*) FROM core.audit_events WHERE action = 'audit.graph_reads_exported'")).scalar_one() == 1
        conn.execute(text("INSERT INTO core.graph_read_log (company_id, actor_user_id, occurred_at, operation, depth, outcome)"
                          " VALUES (:c, :u, now() - interval '91 days', 'neighbourhood', 2, 'ok')"),
                     {"c": demo, "u": user_id("jonas.becker@bina.example")})
    with pytest.raises(ProgrammingError, match="permission denied"):            # no update grant at all
        with transaction(demo) as conn:
            conn.execute(text("UPDATE core.graph_read_log SET outcome = 'ok'"))
    with transaction(demo, retention_job=True) as conn:                          # the policy keeps young rows
        assert conn.execute(text("DELETE FROM core.graph_read_log WHERE occurred_at > now() - interval '1 day'")).rowcount == 0
    with transaction(demo) as conn:                                               # and nothing outside the retention job
        assert conn.execute(text("DELETE FROM core.graph_read_log")).rowcount == 0
    assert purge_company(demo).graph_reads_deleted == 1                           # only the 91-day-old row
    assert len(owner.get("/api/admin/graph-read-log", params=window).json()) == 4


def test_graph_evaluation_cases(projected, demo):
    report = run_cases(demo)                                            # GRAPH-09
    assert report["leaks"] == [] and report["failures"] == [], report["failures"]
    assert report["groups"] == {"correct": {"n": 6, "passed": 6}, "false": {"n": 4, "passed": 4},
                                "restricted": {"n": 7, "passed": 7}}
