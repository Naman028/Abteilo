"""Four-department MVP acceptance: owner-console operations change real behaviour."""

import json

from fastapi.testclient import TestClient
from sqlalchemy import text

from abteilo.ai.providers import PROVIDERS
from tests.conftest import PASSWORD, department_id, owner_conn, user_id

scripted = PROVIDERS["scripted"]


def test_overview_budget_pressure_and_pending_counts(login):
    owner, jonas, sara = login("owner@bina.example"), login("jonas.becker@bina.example"), login("sara.koch@bina.example")
    eng, fin = department_id(jonas, "Engineering"), department_id(sara, "Finance")
    # Seeded: Engineering budget 50,000 EUR with one 2,400 EUR request pending; 80% threshold = 40,000 EUR.
    assert all(b["department_name"] != "Engineering" for b in owner.get("/api/admin/overview").json()["budget_pressure"])
    assert jonas.post("/api/purchase-requests", json={"department_id": eng, "title": "Climate chamber", "justification": "A17",
                                                       "amount_cents": 3_800_000, "period": "2026-Q4"}).status_code == 201
    o = owner.get("/api/admin/overview").json()
    pressure = {b["department_name"]: b for b in o["budget_pressure"]}
    assert pressure["Engineering"]["pending_cents"] == 4_040_000 and pressure["Engineering"]["approved_cents"] == 0
    assert o["pending"]["purchase_requests"] == 2 and o["pending"]["purchase_amount_cents"] == 4_040_000
    rig = next(r for r in sara.get(f"/api/finance/{fin}/purchase-requests", params={"status": "submitted"}).json()
               if r["title"] == "Thermal test rig")
    sara.post(f"/api/finance/{fin}/purchase-requests/{rig['id']}/decision", json={"decision": "approve"})
    o = owner.get("/api/admin/overview").json()
    assert o["pending"]["purchase_requests"] == 1
    assert {b["department_name"]: b for b in o["budget_pressure"]}["Engineering"]["approved_cents"] == 240_000


def test_switching_a_module_off_removes_screens_and_assistant_tools(login):
    owner, lena = login("owner@bina.example"), login("lena.weber@bina.example")
    eng = department_id(owner, "Engineering")
    assert lena.get(f"/api/engineering/{eng}/projects").status_code == 200
    assert owner.put(f"/api/admin/departments/{eng}/modules", json={"modules": ["assistant", "documents"]}).status_code == 200
    assert lena.get(f"/api/engineering/{eng}/projects").status_code == 404
    scripted.replies.extend([json.dumps({"tool_calls": [{"tool": "engineering.get_project", "arguments": {"project_code": "A17"}}]}),
                             json.dumps({"summary": "No data.", "claims": []})])
    body = lena.post("/api/assistant/messages", json={"department_id": eng, "message": "Status of A17?"}).json()
    assert "engineering.get_project" not in scripted.calls[0]["system"]   # not even offered to the model
    assert "engineering.get_project" not in body["tools_used"]             # and never executed


def test_model_routing_is_applied_and_failures_are_safe(login):
    owner, lena = login("owner@bina.example"), login("lena.weber@bina.example")
    eng = department_id(owner, "Engineering")
    with owner_conn() as conn:
        conn.execute(text("INSERT INTO core.model_profiles (company_id, code, display_name, provider, model_name)"
                          " VALUES (core.current_company_id(), 'eng_special', 'Engineering specialist (test)', 'scripted', 'scripted')"))
    assert owner.put("/api/admin/model-routes", json={"department_id": eng, "task_class": "general",
                                                       "profile_code": "eng_special"}).status_code == 200

    def ask():
        scripted.replies.extend([json.dumps({"tool_calls": []}), json.dumps({"summary": "ok", "claims": []})])
        return lena.post("/api/assistant/messages", json={"department_id": eng, "message": "hello"})

    assert ask().json()["model_profile"] == "Engineering specialist (test)"
    owner.delete("/api/admin/model-routes", params={"department_id": eng, "task_class": "general"})
    assert ask().json()["model_profile"] == "Scripted test model"          # company default again
    owner.patch("/api/admin/models/scripted", json={"enabled": False})
    failed = lena.post("/api/assistant/messages", json={"department_id": eng, "message": "hello"})
    assert failed.status_code == 503 and failed.json()["error"]["code"] == "MODEL_UNAVAILABLE"
    assert "scripted" not in failed.text.lower().replace("model_unavailable", "")  # no internals in the error


def test_disabling_and_enabling_an_account_controls_sign_in(login, app):
    owner = login("owner@bina.example")
    lena_id = user_id("lena.weber@bina.example")
    owner.patch(f"/api/admin/members/{lena_id}", json={"status": "disabled"})
    client = TestClient(app)
    denied = client.post("/api/auth/login", json={"email": "lena.weber@bina.example", "password": PASSWORD})
    assert denied.json()["error"]["code"] == "AUTH_INVALID_CREDENTIALS"     # no hint that the account exists
    owner.patch(f"/api/admin/members/{lena_id}", json={"status": "active"})
    assert login("lena.weber@bina.example").get("/api/me").status_code == 200
    actions = [r["action"] for r in owner.get("/api/admin/audit", params={"action_prefix": "member"}).json()]
    assert actions.count("member.updated") == 2


def test_one_local_model_by_default_and_admin_overrides_survive_the_migration(login, app):
    """New installs route every department's general and heavy work to qwen2.5:7b. Migration 0007 removes the
    old seeded Engineering heavy override (qwen2.5-coder) but keeps one an administrator set deliberately."""
    from abteilo import company
    from abteilo.templates import MAJOR_DEPARTMENTS
    from tests.conftest import _alembic

    owner = login("owner@bina.example")
    from abteilo.templates import DEFAULT_ROUTES
    assert {(d, t, p) for d, t, p in DEFAULT_ROUTES} == {(None, "general", "general_local"), (None, "heavy", "general_local")}
    assert not [r for r in owner.get("/api/admin/model-routes").json() if r["department_name"]]   # no department override
    nord = company.create_company(name="Nordwerk AG (test)", slug="nordwerk", industry="manufacturing",
                                  owner_email="owner@nordwerk.example", owner_name="Nord Owner", owner_password=PASSWORD,
                                  department_templates=list(MAJOR_DEPARTMENTS))
    _alembic("downgrade", "0006")                    # both companies get the old seeded override back
    nowner = TestClient(app)
    nowner.headers["X-Abteilo-CSRF"] = "1"
    assert nowner.post("/api/auth/login", json={"email": "owner@nordwerk.example", "password": PASSWORD}).status_code == 200
    nord_eng = nord.department_ids["engineering"]     # Nordwerk's admin deliberately keeps the coder model
    assert nowner.put("/api/admin/model-routes", json={"department_id": str(nord_eng), "task_class": "heavy",
                                                        "profile_code": "engineering_local"}).status_code == 200
    _alembic("upgrade")
    assert not [r for r in owner.get("/api/admin/model-routes").json() if r["department_name"]]   # BINA: seeded one removed
    assert [(r["task_class"], r["profile_code"]) for r in nowner.get("/api/admin/model-routes").json() if r["department_name"]] \
        == [("heavy", "engineering_local")]                                                    # Nordwerk: admin choice kept
    with owner_conn() as conn:
        audit = conn.execute(text("SELECT details FROM core.audit_events WHERE action = 'model.route_reset'")).scalars().all()
    assert [a["migration"] for a in audit] == ["0007"] and audit[0]["previous_profile"] == "engineering_local"
