"""Authorization boundaries and the approval workflows, through the HTTP API."""

import threading

from sqlalchemy import text

from tests.conftest import department_id, owner_conn, user_id


def test_login_errors_are_uniform_and_throttled(app, demo):
    from fastapi.testclient import TestClient

    client = TestClient(app)
    unknown = client.post("/api/auth/login", json={"email": "nobody@bina.example", "password": "x"})
    wrong = client.post("/api/auth/login", json={"email": "jonas.becker@bina.example", "password": "wrong-password-1"})
    assert unknown.json()["error"]["code"] == wrong.json()["error"]["code"] == "AUTH_INVALID_CREDENTIALS"
    for _ in range(4):
        client.post("/api/auth/login", json={"email": "jonas.becker@bina.example", "password": "wrong-password-1"})
    throttled = client.post("/api/auth/login", json={"email": "jonas.becker@bina.example", "password": "test-password-123"})
    assert throttled.status_code == 429


def test_writes_require_csrf_header(login):
    jonas = login("jonas.becker@bina.example")
    del jonas.headers["X-Abteilo-CSRF"]
    r = jonas.post("/api/hr/leave-requests", json={"start_date": "2026-12-01", "end_date": "2026-12-02"})
    assert r.json()["error"]["code"] == "CSRF_REQUIRED"


def test_department_boundaries(login):
    jonas, owner, mila = login("jonas.becker@bina.example"), login("owner@bina.example"), login("mila.hoffmann@bina.example")
    eng, fin = department_id(jonas, "Engineering"), department_id(jonas, "Finance")
    assert jonas.get(f"/api/engineering/{eng}/projects").status_code == 200
    assert jonas.get(f"/api/finance/{fin}/budgets").status_code == 403
    assert jonas.get("/api/admin/members").status_code == 403
    # The owner runs the company but does not automatically read department records (SEC-04).
    assert owner.get(f"/api/engineering/{eng}/projects").status_code == 403
    # Confidential documents are visible only with the confidential permission in their department.
    def titles(client):
        return {h["document_title"] for h in client.get("/api/documents-search", params={"q": "salary bands"}).json()["hits"]}
    assert "Salary Bands 2026" not in titles(jonas)
    assert "Salary Bands 2026" in titles(mila)


def test_purchase_request_workflow_and_budget(login):
    jonas, sara = login("jonas.becker@bina.example"), login("sara.koch@bina.example")
    eng, fin = department_id(jonas, "Engineering"), department_id(sara, "Finance")
    big = jonas.post("/api/purchase-requests", json={"department_id": eng, "title": "Climate chamber",
                                                      "justification": "A17 retest", "amount_cents": 4_900_000,
                                                      "period": "2026-Q4"})
    assert big.status_code == 201
    pending = sara.get(f"/api/finance/{fin}/purchase-requests", params={"status": "submitted"}).json()
    rig = next(r for r in pending if r["title"] == "Thermal test rig")
    assert jonas.post(f"/api/finance/{fin}/purchase-requests/{rig['id']}/decision", json={"decision": "approve"}).status_code == 403
    assert sara.post(f"/api/finance/{fin}/purchase-requests/{rig['id']}/decision", json={"decision": "approve"}).json()["status"] == "approved"
    # 50,000 budget - 2,400 approved leaves 47,600: a 49,000 approval must be refused.
    over = sara.post(f"/api/finance/{fin}/purchase-requests/{big.json()['id']}/decision", json={"decision": "approve"})
    assert over.status_code == 409


def test_concurrent_approvals_cannot_overspend(login):
    jonas, sara = login("jonas.becker@bina.example"), login("sara.koch@bina.example")
    eng, fin = department_id(jonas, "Engineering"), department_id(sara, "Finance")
    ids = [jonas.post("/api/purchase-requests", json={"department_id": eng, "title": f"Machine {i}", "justification": "x",
                                                       "amount_cents": 3_000_000, "period": "2026-Q4"}).json()["id"]
           for i in range(2)]
    barrier, results = threading.Barrier(2), []

    def approve(request_id: str) -> None:
        barrier.wait()
        results.append(sara.post(f"/api/finance/{fin}/purchase-requests/{request_id}/decision",
                                 json={"decision": "approve"}).status_code)

    threads = [threading.Thread(target=approve, args=(i,)) for i in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200, 409]  # 2 x 30,000 > 50,000: exactly one wins, whatever the interleaving


def test_access_change_needs_a_second_person(login):
    tim, eva, jonas = login("tim.schulz@bina.example"), login("eva.richter@bina.example"), login("jonas.becker@bina.example")
    it_dept, eng = department_id(tim, "IT & Security"), department_id(tim, "Engineering")
    proposal = tim.post(f"/api/it/{it_dept}/access-requests", json={
        "target_user_id": str(user_id("jonas.becker@bina.example")), "role_code": "engineering_lead",
        "department_id": eng, "justification": "Covers project lead duties"}).json()
    assert tim.post(f"/api/it/access-requests/{proposal['id']}/decision", json={"decision": "approve"}).status_code == 403
    assert jonas.post(f"/api/it/access-requests/{proposal['id']}/decision", json={"decision": "approve"}).status_code == 403
    assert eva.post(f"/api/it/access-requests/{proposal['id']}/decision", json={"decision": "approve"}).json()["status"] == "approved"
    jonas_again = login("jonas.becker@bina.example")
    eng_ws = next(w for w in jonas_again.me["workspaces"] if w["code"] == "engineering")
    assert "engineering.project.manage" in eng_ws["permissions"]
    # A security admin cannot approve a change that targets themselves.
    self_target = tim.post(f"/api/it/{it_dept}/access-requests", json={
        "target_user_id": str(user_id("eva.richter@bina.example")), "role_code": "company_owner",
        "justification": "x"}).json()
    assert eva.post(f"/api/it/access-requests/{self_target['id']}/decision", json={"decision": "approve"}).status_code == 403


def test_owner_adds_department_without_code_changes(login):
    owner = login("owner@bina.example")
    r = owner.post("/api/admin/departments", json={"code": "quality", "name": "Quality Assurance", "template": "custom"})
    assert r.status_code == 201 and r.json()["modules"] == ["assistant", "documents"]
    member = owner.post("/api/admin/members", json={
        "email": "qa.lead@bina.example", "display_name": "QA Lead", "initial_password": "test-password-123",
        "department_id": r.json()["id"], "department_role": "department_lead"})
    assert member.status_code == 201
    qa = login("qa.lead@bina.example")
    assert [w["code"] for w in qa.me["workspaces"]] == ["quality"]
    # Archiving keeps every record (DATA-01) and removes the workspace.
    owner.patch(f"/api/admin/departments/{r.json()['id']}", json={"status": "archived"})
    assert login("qa.lead@bina.example").me["workspaces"] == []
    with owner_conn() as conn:
        assert conn.execute(text("SELECT status FROM core.departments WHERE code = 'quality'")).scalar_one() == "archived"


def test_elevated_roles_need_the_owners_approval(login):
    tim, eva, owner = login("tim.schulz@bina.example"), login("eva.richter@bina.example"), login("owner@bina.example")
    it_dept = department_id(tim, "IT & Security")
    request = tim.post(f"/api/it/{it_dept}/access-requests", json={
        "target_user_id": str(user_id("jonas.becker@bina.example")), "role_code": "company_owner",
        "justification": "Co-founder joins"}).json()
    assert request["elevated"] is True
    # A security administrator approves ordinary changes but not owner grants.
    assert eva.post(f"/api/it/access-requests/{request['id']}/decision", json={"decision": "approve"}).status_code == 403
    overview = owner.get("/api/admin/overview").json()
    assert [r["id"] for r in overview["elevated_requests"]] == [request["id"]]
    assert owner.post(f"/api/it/access-requests/{request['id']}/decision", json={"decision": "approve"}).status_code == 200
    assert "role.elevated.approve" in login("jonas.becker@bina.example").me["company_permissions"]


def test_owner_overview_shows_what_needs_attention(login):
    owner = login("owner@bina.example")
    o = owner.get("/api/admin/overview").json()
    assert {b["code"] for b in o["blocked_projects"]} == {"A17"}
    assert o["blocked_projects"][0]["blocked_tasks"] == ["Thermal validation retest"]
    assert o["pending"]["purchase_requests"] == 1 and o["pending"]["leave_requests"] == 1
    assert o["pending"]["access_requests"] == 1
    assert login("jonas.becker@bina.example").get("/api/admin/overview").status_code == 403


def test_owner_manages_people_departments_and_routes(login):
    owner, jonas = login("owner@bina.example"), login("jonas.becker@bina.example")
    jonas_id, eng, fin = user_id("jonas.becker@bina.example"), department_id(owner, "Engineering"), department_id(owner, "Finance")
    # Department roles can be assigned directly; company roles only through the access workflow.
    assert owner.post(f"/api/admin/members/{jonas_id}/roles", json={"department_id": fin, "role_code": "department_member"}).status_code == 201
    assert owner.post(f"/api/admin/members/{jonas_id}/roles", json={"department_id": fin, "role_code": "security_admin"}).status_code == 422
    roles = owner.get("/api/admin/members").json()
    fin_role = next(r for m in roles if m["user_id"] == str(jonas_id) for r in m["roles"] if r["department_id"] == fin)
    assert owner.delete(f"/api/admin/members/{jonas_id}/roles/{fin_role['assignment_id']}").status_code == 200
    # A password reset signs the person out everywhere.
    assert owner.post(f"/api/admin/members/{jonas_id}/password", json={"new_password": "another-password-1"}).status_code == 204
    assert jonas.get("/api/me").status_code == 401
    # Modules can only be switched within what the department's template supports.
    assert owner.put(f"/api/admin/departments/{fin}/modules", json={"modules": ["finance", "projects"]}).status_code == 422
    assert owner.put(f"/api/admin/departments/{fin}/modules", json={"modules": ["finance", "documents"]}).json()["modules"] == ["documents", "finance"]
    # A department override can be removed so the company default applies again.
    routes = owner.delete("/api/admin/model-routes", params={"department_id": eng, "task_class": "heavy"}).json()
    assert all(r["department_id"] is None for r in routes)
    assert owner.get("/api/admin/audit.csv").headers["content-type"].startswith("text/csv")
