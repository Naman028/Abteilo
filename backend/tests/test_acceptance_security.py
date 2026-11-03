"""Four-department MVP acceptance: ownership, authorisation matrix, sessions,
pooled-connection context and audit export safety."""

import csv
import io
import threading
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from abteilo import reset_password
from abteilo.admin import set_member_status
from abteilo.db import get_engine, set_clause, transaction
from abteilo.main import API_ROUTERS
from tests.conftest import PASSWORD, department_id, owner_conn, user_id

OWNER, LENA, JONAS, EVA = ("owner@bina.example", "lena.weber@bina.example", "jonas.becker@bina.example",
                           "eva.richter@bina.example")


def api_routes():
    """Every /api route, read from the routers the app includes (stable across FastAPI's route nesting)."""
    return [route for router in API_ROUTERS for route in router.routes]


def _make_owner(email: str) -> None:
    with owner_conn() as conn:
        conn.execute(text("INSERT INTO core.role_assignments (id, company_id, user_id, role_code)"
                          " VALUES (:id, core.current_company_id(), :u, 'company_owner')"),
                     {"id": uuid4(), "u": user_id(email)})


def _active_owners() -> int:
    with owner_conn() as conn:
        return conn.execute(text(
            "SELECT count(*) FROM core.role_assignments ra JOIN core.memberships m ON m.company_id = ra.company_id"
            " AND m.user_id = ra.user_id WHERE ra.role_code = 'company_owner' AND ra.revoked_at IS NULL"
            " AND m.status = 'active'")).scalar_one()


def _wait_until_blocked_on_lock(timeout: float = 15.0) -> None:
    """Condition wait (not a fixed sleep): return once another session waits on a row lock."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with get_engine().connect() as conn:
            if conn.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                                 " AND wait_event_type = 'Lock'")).scalar_one():
                return
        time.sleep(0.02)
    pytest.fail("second transaction never waited on the company lock")


# --- OWN-01: at least one active owner, under concurrency -----------------------------------
def test_concurrent_owner_removals_keep_one_active_owner(login, demo):
    _make_owner(LENA)
    lena = login(LENA)
    anna_id, lena_id = user_id(OWNER), user_id(LENA)
    with owner_conn() as conn:
        lena_owner_assignment = conn.execute(text(
            "SELECT id FROM core.role_assignments WHERE user_id = :u AND role_code = 'company_owner'"), {"u": lena_id}).scalar_one()

    # Transaction 1 (Anna): revoke Lena's owner role, but do not commit yet.
    t1 = transaction(demo, anna_id)
    conn1 = t1.__enter__()
    from abteilo.authz import load_grants, Principal
    from abteilo.modules.it import revoke_assignment
    anna = Principal(anna_id, demo, OWNER, "Anna", load_grants(conn1, demo, anna_id))
    revoke_assignment(conn1, anna, lena_owner_assignment)

    # Transaction 2 (Lena, concurrently): disable Anna's account.
    result: dict = {}
    worker = threading.Thread(target=lambda: result.update(
        r=lena.patch(f"/api/admin/members/{anna_id}", json={"status": "disabled"})))
    worker.start()
    _wait_until_blocked_on_lock()      # proven: T2 is queued behind T1's company-row lock
    t1.__exit__(None, None, None)      # commit T1
    worker.join()

    assert result["r"].status_code == 409   # T2 re-checked after T1 committed and refused
    assert _active_owners() == 1


def test_owner_cannot_be_removed_through_any_path(login, demo):
    owner = login(OWNER)
    anna_id = user_id(OWNER)
    with owner_conn() as conn:
        anna_owner = conn.execute(text("SELECT id FROM core.role_assignments WHERE user_id = :u AND role_code = 'company_owner'"),
                                  {"u": anna_id}).scalar_one()
    # Self-service removal is refused; and the service-level guard holds even for a direct call.
    assert owner.patch(f"/api/admin/members/{anna_id}", json={"status": "disabled"}).status_code == 403
    assert owner.delete(f"/api/admin/members/{anna_id}/roles/{anna_owner}").status_code == 403
    from abteilo.errors import Conflict
    with pytest.raises(Conflict):
        with transaction(demo, anna_id) as conn:
            set_member_status(conn, anna_id, "disabled")
    assert _active_owners() == 1


# --- SEC-06: elevated roles only through owner-approved access requests -----------------------
def test_no_endpoint_grants_elevated_roles_directly(login):
    owner, tim, eva = login(OWNER), login("tim.schulz@bina.example"), login(EVA)
    jonas_id, fin = str(user_id(JONAS)), department_id(owner, "Finance")
    for role in ("company_owner", "security_admin", "employee"):
        assert owner.post(f"/api/admin/members/{jonas_id}/roles", json={"department_id": fin, "role_code": role}).status_code == 422
    created = owner.post("/api/admin/members", json={"email": "new@bina.example", "display_name": "New Person",
                                                      "initial_password": PASSWORD, "department_id": fin,
                                                      "department_role": "company_owner"})
    assert created.status_code == 422
    # A company role on a department (or the reverse) is rejected at proposal time.
    it_dept = department_id(tim, "IT & Security")
    assert tim.post(f"/api/it/{it_dept}/access-requests", json={"target_user_id": jonas_id, "role_code": "company_owner",
                                                                "department_id": fin, "justification": "x"}).status_code == 422
    request = tim.post(f"/api/it/{it_dept}/access-requests", json={"target_user_id": jonas_id, "role_code": "security_admin",
                                                                   "justification": "x"}).json()
    assert eva.post(f"/api/it/access-requests/{request['id']}/decision", json={"decision": "approve"}).status_code == 403
    assert "it.access.approve" not in login(JONAS).me["company_permissions"]


# --- Permission matrix for every administrative endpoint -----------------------------------
def _admin_matrix(ids: dict) -> list[tuple[str, str, str, dict | None, dict | None]]:
    """(method, path, required permission, json body, query params). Every /api/admin route must appear."""
    eng, fin, lena, lena_role = ids["eng"], ids["fin"], ids["lena"], ids["lena_role"]
    return [
        ("GET", "/api/admin/overview", "report.executive.read", None, None),
        ("GET", "/api/admin/audit", "audit.read", None, None),
        ("GET", "/api/admin/audit.csv", "audit.read", None, None),
        ("GET", "/api/admin/graph-read-log", "audit.read", None,
         {"since": "2026-01-01T00:00:00Z", "until": "2027-01-01T00:00:00Z"}),
        ("GET", "/api/admin/usage", "ai.usage.read", None, None),
        ("GET", "/api/admin/settings", "company.settings.manage", None, None),
        ("PUT", "/api/admin/settings", "company.settings.manage", {"chat_retention_days": None, "external_ai_enabled": False}, None),
        ("PUT", "/api/admin/company", "company.settings.manage", {"name": "BINA GmbH (demo)", "timezone": "Europe/Berlin"}, None),
        ("GET", "/api/admin/data", "company.settings.manage", None, None),
        ("POST", "/api/admin/retention/run", "company.settings.manage", {}, None),
        ("POST", "/api/admin/index/retry", "company.settings.manage", {}, None),
        ("GET", "/api/admin/graph", "company.settings.manage", None, None),
        ("POST", "/api/admin/graph/rebuild", "company.settings.manage", {}, None),
        ("GET", "/api/admin/department-templates", "department.manage", None, None),
        ("GET", "/api/admin/departments", "department.manage", None, None),
        ("POST", "/api/admin/departments", "department.manage", {"code": "quality", "name": "Quality", "template": "custom"}, None),
        ("PATCH", f"/api/admin/departments/{eng}", "department.manage", {"name": "Engineering"}, None),
        ("GET", f"/api/admin/departments/{eng}/members", "department.manage", None, None),
        ("PUT", f"/api/admin/departments/{eng}/modules", "department.manage", {"modules": ["assistant", "documents", "projects"]}, None),
        ("GET", "/api/admin/members", "member.manage", None, None),
        ("POST", "/api/admin/members", "member.manage", {"email": "m@bina.example", "display_name": "Matrix Person",
                                                          "initial_password": PASSWORD}, None),
        ("PATCH", f"/api/admin/members/{lena}", "member.manage", {"title": "Engineering Lead"}, None),
        ("POST", f"/api/admin/members/{lena}/roles", "member.manage", {"department_id": fin, "role_code": "department_member"}, None),
        # Revoking an ordinary role: member.manage or it.access.approve (security administrators).
        ("DELETE", f"/api/admin/members/{lena}/roles/{lena_role}", "member.manage|it.access.approve", None, None),
        ("POST", f"/api/admin/members/{lena}/password", "member.manage", {"new_password": "matrix-password-1"}, None),
        ("GET", "/api/admin/models", "model.manage", None, None),
        ("POST", "/api/admin/models", "model.manage", {"code": "extra_local", "display_name": "Extra", "provider": "ollama",
                                                        "model_name": "llama3.2"}, None),
        ("PATCH", "/api/admin/models/general_local", "model.manage", {"purpose": "Default model"}, None),
        ("POST", "/api/admin/models/scripted/test", "model.manage", {}, None),
        ("GET", "/api/admin/model-providers", "model.manage", None, None),
        ("GET", "/api/admin/model-routes", "model.manage", None, None),
        ("PUT", "/api/admin/model-routes", "model.manage", {"department_id": None, "task_class": "general", "profile_code": "scripted"}, None),
        ("DELETE", "/api/admin/model-routes", "model.manage", None, {"department_id": eng, "task_class": "heavy"}),
    ]


def test_admin_permission_matrix(login):
    owner, eva, jonas = login(OWNER), login(EVA), login(JONAS)
    with owner_conn() as conn:
        lena_role = conn.execute(text("SELECT id FROM core.role_assignments WHERE user_id = :u AND role_code = 'engineering_lead'"),
                                 {"u": user_id(LENA)}).scalar_one()
    ids = {"eng": department_id(owner, "Engineering"), "fin": department_id(owner, "Finance"),
           "lena": str(user_id(LENA)), "lena_role": str(lena_role)}
    matrix = _admin_matrix(ids)

    covered = {(m, p.replace(ids["eng"], "{department_id}").replace(ids["lena_role"], "{assignment_id}")
                .replace(ids["lena"], "{user_id}").replace("general_local", "{code}").replace("scripted", "{code}"))
               for m, p, *_ in matrix}
    routes = {(m, r.path) for r in api_routes() if r.path.startswith("/api/admin") for m in r.methods - {"HEAD"}}
    assert routes == covered, f"admin routes without an expected permission: {routes - covered}"

    def call(client, method, path, body, params):
        return client.request(method, path, json=body, params=params)

    eva_perms = set(login(EVA).me["company_permissions"])
    for method, path, perm, body, params in matrix:                # denials first: no state changes yet
        assert call(jonas, method, path, body, params).status_code == 403, (method, path)
        if not set(perm.split("|")) & eva_perms:
            assert call(eva, method, path, body, params).status_code == 403, (method, path)
    for method, path, perm, body, params in matrix:                # then every permitted holder succeeds
        holder = eva if set(perm.split("|")) & eva_perms else owner
        status = call(holder, method, path, body, params).status_code
        assert status < 400 or status == 404, (method, path, status)


# --- Sessions are invalid everywhere after reset or disable ----------------------------------
@pytest.mark.parametrize("action", ["password_reset", "operator_reset", "account_disabled"])
def test_old_sessions_are_rejected_by_every_protected_route(login, app, action):
    owner, jonas = login(OWNER), login(JONAS)
    jonas_id = user_id(JONAS)
    if action == "password_reset":
        assert owner.post(f"/api/admin/members/{jonas_id}/password", json={"new_password": "brand-new-password"}).status_code == 204
    elif action == "operator_reset":
        # Break-glass CLI path (python -m abteilo.reset_password): old password out, new one in, audited.
        assert reset_password.reset(JONAS.upper(), "brand-new-password")
        fresh = TestClient(app)
        assert fresh.post("/api/auth/login", json={"email": JONAS, "password": PASSWORD}).status_code == 401
        assert fresh.post("/api/auth/login", json={"email": JONAS, "password": "brand-new-password"}).status_code == 200
        with owner_conn() as conn:
            assert conn.execute(text("SELECT count(*) FROM core.audit_events WHERE action = 'member.password_reset'"
                                     " AND resource_id = :u AND details->>'via' = 'operator_cli'"),
                                {"u": str(jonas_id)}).scalar() == 1
    else:
        assert owner.patch(f"/api/admin/members/{jonas_id}", json={"status": "disabled"}).status_code == 200
    public = {"/api/auth/login", "/api/auth/logout", "/api/docs", "/api/openapi.json", "/api/docs/oauth2-redirect"}
    checked = 0
    for route in api_routes():
        path = route.path
        if not path.startswith("/api/") or path in public:
            continue
        concrete = path
        for name in ("department_id", "project_id", "task_id", "request_id", "assignment_id", "user_id",
                     "conversation_id", "document_id"):
            concrete = concrete.replace("{" + name + "}", str(uuid4()))
        concrete = concrete.replace("{code}", "general_local")
        for method in route.methods - {"HEAD"}:
            r = jonas.request(method, concrete, json={}, params={"q": "test"})
            assert r.status_code == 401, (method, path, r.status_code)
            checked += 1
    assert checked > 60  # every protected route was exercised, not a sample


def test_self_service_password_change(login, app):
    here, elsewhere = login(JONAS), login(JONAS)
    wrong = here.post("/api/me/password", json={"current_password": "not-my-password", "new_password": "brand-new-password"})
    assert wrong.status_code == 422 and wrong.json()["error"]["code"] == "AUTH_CURRENT_PASSWORD_INCORRECT"
    assert here.post("/api/me/password", json={"current_password": PASSWORD, "new_password": "brand-new-password"}).status_code == 204
    assert here.get("/api/me").status_code == 200          # this session stays signed in
    assert elsewhere.get("/api/me").status_code == 401     # every other session is signed out
    fresh = TestClient(app)
    assert fresh.post("/api/auth/login", json={"email": JONAS, "password": PASSWORD}).status_code == 401
    assert fresh.post("/api/auth/login", json={"email": JONAS, "password": "brand-new-password"}).status_code == 200
    with owner_conn() as conn:
        assert conn.execute(text("SELECT count(*) FROM core.audit_events WHERE action = 'auth.password_changed'"
                                 " AND resource_id = :u"), {"u": str(user_id(JONAS))}).scalar() == 1


def test_wrong_current_password_counts_toward_the_login_throttle(login):
    jonas = login(JONAS)
    for _ in range(5):
        jonas.post("/api/me/password", json={"current_password": "guess-guess-guess", "new_password": "brand-new-password"})
    r = jonas.post("/api/me/password", json={"current_password": PASSWORD, "new_password": "brand-new-password"})
    assert r.status_code == 429  # a stolen session cannot be used to guess the password


# --- Pooled connections never carry tenant or user context ------------------------------------
def test_pooled_connections_do_not_leak_context(demo):
    anna = user_id(OWNER)
    with transaction(demo, anna, retention_job=True) as conn:
        assert conn.execute(text("SELECT current_setting('app.company_id', true)")).scalar() == str(demo)
    with pytest.raises(RuntimeError):
        with transaction(demo, anna):
            raise RuntimeError("rolled back mid-request")
    for _ in range(get_engine().pool.size() + 2):   # cycle through every pooled connection
        with transaction() as conn:
            settings = conn.execute(text("SELECT current_setting('app.company_id', true), current_setting('app.user_id', true),"
                                         " current_setting('app.retention_job', true)")).one()
            assert all(s in ("", None) for s in settings)
            assert conn.execute(text("SELECT count(*) FROM core.departments")).scalar_one() == 0


# --- Dynamic UPDATE column lists ------------------------------------------------------------------
def test_set_clause_binds_values_and_refuses_non_identifier_columns():
    assert set_clause({"name": "x", "status": "archived"}) == "name = :name, status = :status"
    with pytest.raises(ValueError):
        set_clause({"name = 'x' --": "y"})


# --- Audit export: authorisation and spreadsheet formula injection ------------------------------
def test_audit_csv_is_authorised_and_formula_safe(login):
    eva, jonas = login(EVA), login(JONAS)
    hostile = ['=HYPERLINK("http://evil.example","x")', "+cmd|' /C calc'!A0", "-2+3", "@SUM(1,2)", "\t=1+1",
               'Name with "quotes", commas; and semicolons']
    with owner_conn() as conn:
        for value in hostile:
            conn.execute(text("INSERT INTO core.audit_events (company_id, actor_user_id, action, resource_type, resource_id)"
                              " VALUES (core.current_company_id(), NULL, 'test.event', 'test', :v)"), {"v": value})
    assert jonas.get("/api/admin/audit.csv").status_code == 403
    r = eva.get("/api/admin/audit.csv")          # security administrators hold audit.read
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(r.text)))
    cells = {row[5] for row in rows[1:]}
    for value in hostile:
        expected = "'" + value if value[0] in "=+-@\t" else value
        assert expected in cells, value       # re-opened file yields the exact, neutralised text
