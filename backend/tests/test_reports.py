"""Reports: request detection, deterministic completeness, verification, ownership, revocation, expiry,
answer exports and safe rendering."""

import io
import json
import zipfile
from datetime import UTC, datetime

from sqlalchemy import text

from abteilo.ai.providers import PROVIDERS
from abteilo.reports import Report, Section, Statement, render_docx, render_pdf, requested_format
from abteilo.report_data import Table
from abteilo.retention import purge_company
from tests.conftest import department_id, owner_conn, user_id

scripted = PROVIDERS["scripted"]
LENA, JONAS, OWNER = "lena.weber@bina.example", "jonas.becker@bina.example", "owner@bina.example"


def _docx_text(content: bytes) -> str:
    return zipfile.ZipFile(io.BytesIO(content)).read("word/document.xml").decode()


def _generate(client, dept, message, narrative):
    scripted.replies.clear()
    scripted.calls.clear()
    scripted.replies.append(json.dumps(narrative))
    return client.post("/api/assistant/messages", json={"department_id": dept, "message": message}).json()


NARRATIVE = {
    "summary": "Project A17 is complete.", "summary_evidence_ids": ["E1"],          # contradicts the records
    "observations": [{"text": "Project A17 is blocked.", "evidence_ids": ["E2"], "type": "current_state"},
                     {"text": "Project B24 is done.", "evidence_ids": ["E1"], "type": "current_state"}],
    "next_steps": [{"text": "Follow up with Jonas Becker on the Thermal validation retest.", "evidence_ids": ["E2"]},
                   {"text": "Approve 99999 EUR for project A17.", "evidence_ids": ["E2"]},
                   {"text": "Escalate project A17 because the retest is blocked.", "evidence_ids": ["E2"]}],
}


def test_report_requests_are_detected_and_ordinary_questions_are_not():
    assert requested_format("make me a pdf file for this") == "pdf"
    assert requested_format("Create a project progress report") == "pdf"
    assert requested_format("Make me a Word report of project A17") == "docx"
    assert requested_format("Erstelle einen Statusbericht") == "pdf"
    assert requested_format("What is the status of A17?") is None
    assert requested_format("In other words, what is blocked?") is None


def test_report_covers_every_record_and_holds_only_checked_statements(login):
    lena = login(LENA)
    eng = department_id(lena, "Engineering")
    reply = _generate(lena, eng, "Make me a Word report of the project progress", NARRATIVE)
    assert reply["report"]["complete"] is True and reply["report"]["format"] == "docx"
    assert len(scripted.calls) == 1                  # one narrative call, no planning: the model chooses no data
    xml = _docx_text(lena.get(f"/api/reports/{reply['report']['id']}", params={"format": "docx"}).content)
    # Completeness comes from the records, not from what the model chose to mention.
    for expected in ("A17", "B24", "C08", "Thermal validation retest", "Order replacement cooling module",
                     "Connect line 2 PLC data", "Downtime alert thresholds", "Jonas Becker", "14.11.2026"):
        assert expected in xml, expected
    assert "Computed directly from the records" in xml and "A17 is complete" not in xml   # unsupported summary replaced
    assert "Project A17 is blocked." in xml and "Project B24 is done." not in xml        # unsupported observation left out
    assert "Follow up with Jonas Becker" in xml and "99999" not in xml                    # unsafe suggestion dropped
    assert "Escalate project A17" not in xml                                              # a reason inside a suggestion
    assert "AI suggestions" in xml and "3 statements the AI wrote could not be verified" in xml
    assert lena.get(f"/api/reports/{reply['report']['id']}").content.startswith(b"%PDF")


def test_reports_are_private_follow_revocation_and_expire(login, demo):
    lena, jonas, owner = login(LENA), login(JONAS), login(OWNER)
    eng = department_id(lena, "Engineering")
    report_id = _generate(lena, eng, "Create a project progress report", NARRATIVE)["report"]["id"]
    url = f"/api/reports/{report_id}"
    assert lena.get(url).status_code == 200
    assert jonas.get(url).status_code == 404 and owner.get(url).status_code == 404   # owner-private, even to the owner
    with owner_conn() as conn:
        details = conn.execute(text("SELECT action, details FROM core.audit_events WHERE resource_id = :r"
                                    " ORDER BY occurred_at"), {"r": report_id}).all()
    assert [a for a, _ in details] == ["report.created", "report.downloaded"]
    assert all(set(d) <= {"kind", "complete", "sections", "format"} for _, d in details)   # no report content in audit

    with owner_conn() as conn:   # Lena loses her Engineering role: the old report must not outlive it
        conn.execute(text("UPDATE core.role_assignments SET revoked_at = now(), revoked_by = :o"
                          " WHERE user_id = :u AND department_id = :d"),
                     {"u": user_id(LENA), "d": eng, "o": user_id(OWNER)})
    revoked = lena.get(url)
    assert revoked.status_code == 403 and revoked.json()["error"]["code"] == "REPORT_ACCESS_REVOKED"

    with owner_conn(user_id(LENA)) as conn:   # expiry: unreadable at once, deleted by the retention job
        conn.execute(text("UPDATE conv.reports SET created_at = now() - interval '40 days',"
                          " expires_at = now() - interval '10 days' WHERE id = :r"), {"r": report_id})
    assert purge_company(demo).reports_deleted == 1
    with owner_conn() as conn:
        assert conn.execute(text("SELECT count(*) FROM conv.reports")).scalar() == 0


def test_answer_export_is_labelled_and_rechecks_access(login):
    jonas = login(JONAS)
    eng = department_id(jonas, "Engineering")
    scripted.replies.clear()
    scripted.replies.extend([
        json.dumps({"tool_calls": [{"tool": "engineering.get_project", "arguments": {"project_code": "A17"}}]}),
        json.dumps({"summary": "A17 is blocked.", "summary_evidence_ids": ["E1"],
                    "claims": [{"text": "Project A17 is blocked.", "evidence_ids": ["E1"], "type": "current_state"}]})])
    reply = jonas.post("/api/assistant/messages", json={"department_id": eng, "message": "What is the status of A17?"}).json()
    assert reply["report"] is None
    url = f"/api/conversations/{reply['conversation_id']}/messages/{reply['message_id']}/export"
    xml = _docx_text(jonas.get(url, params={"format": "docx"}).content)
    assert "This is an export of a chat answer" in xml and "Project A17 is blocked." in xml
    with owner_conn() as conn:
        conn.execute(text("UPDATE core.role_assignments SET revoked_at = now(), revoked_by = :o"
                          " WHERE user_id = :u AND department_id = :d"),
                     {"u": user_id(JONAS), "d": eng, "o": user_id(OWNER)})
    revoked = jonas.get(url)
    assert revoked.status_code == 403 and revoked.json()["error"]["code"] == "EXPORT_ACCESS_REVOKED"


def test_rendering_treats_model_text_as_text():
    hostile = '<b>unclosed <font size="80">x &amp; <para> & </para> ąő→✓ Grüße – 2.400,00 € „Zitat“ ' + "lang " * 400
    report = Report(kind="engineering_progress", title="Engineering progress report", company="BINA GmbH", scope="Engineering",
                    author="Lena Weber", request=hostile[:500], generated_at=datetime.now(UTC), data_as_of=datetime.now(UTC),
                    period="Current state", complete=False, limitations=["Only the first 40 of 41 tasks are listed."],
                    summary=hostile, summary_basis="verified",
                    overview=Table(columns=["Project", "Status"], rows=[[hostile[:60], "blocked"]] * 60),
                    sections=[Section(title=hostile[:80], table=Table(columns=["Task"], rows=[[hostile]]),
                                      notes=[Statement(text=hostile, status="verified", evidence_ids=["E1"])])])
    pdf = render_pdf(report)                                              # unescaped markup would raise here
    assert pdf.startswith(b"%PDF") and pdf.count(b"/Type /Page\n") + pdf.count(b"/Type /Page ") >= 2  # paginated
    xml = _docx_text(render_docx(report))
    assert "&lt;b&gt;unclosed" in xml and "ąő→✓ Grüße" in xml and "PARTIAL" in xml


def test_downloads_stop_for_suspended_accounts_and_revoked_sources(login):
    lena = login(LENA)
    eng = department_id(lena, "Engineering")
    reply = _generate(lena, eng, "Create a project progress report", NARRATIVE)
    url = f"/api/reports/{reply['report']['id']}"
    with owner_conn(user_id(LENA)) as conn:   # a second report of Lena's whose narrative cited a company document
        doc = conn.execute(text("SELECT id, title FROM core.documents WHERE title = 'Engineering Handbook'")).one()
        content = conn.execute(text("SELECT content FROM conv.reports WHERE id = :r"), {"r": reply["report"]["id"]}).scalar()
        content["sources"].append({"id": "E9", "title": doc.title, "kind": "document", "as_of": None,
                                   "source_type": "document", "source_id": str(doc.id)})
        conn.execute(text("INSERT INTO conv.reports (id, company_id, user_id, department_id, kind, title, complete, content,"
                          " expires_at) SELECT gen_random_uuid(), company_id, user_id, department_id, kind, title, complete,"
                          " CAST(:c AS jsonb), expires_at FROM conv.reports WHERE id = :r RETURNING id"),
                     {"c": json.dumps(content), "r": reply["report"]["id"]})
        with_doc = conn.execute(text("SELECT id FROM conv.reports WHERE content::text LIKE '%Engineering Handbook%'")).scalar()
    assert lena.get(f"/api/reports/{with_doc}").status_code == 200
    with owner_conn() as conn:   # the document becomes HR-confidential: Lena may no longer open it
        conn.execute(text("UPDATE core.documents SET classification = 'confidential', department_id ="
                          " (SELECT id FROM core.departments WHERE template = 'human_resources') WHERE id = :d"), {"d": doc.id})
    revoked = lena.get(f"/api/reports/{with_doc}")
    assert revoked.status_code == 403 and revoked.json()["error"]["code"] == "REPORT_ACCESS_REVOKED"
    assert lena.get(url).status_code == 200                                    # the report without that source still works

    for table, status in (("core.memberships", "disabled"), ("core.users", "disabled")):
        with owner_conn() as conn:
            key = "user_id" if table == "core.memberships" else "id"
            conn.execute(text(f"UPDATE {table} SET status = :s WHERE {key} = :u"), {"s": status, "u": user_id(LENA)})
        assert lena.get(url).status_code == 401                                # suspended or disabled: no download
        with owner_conn() as conn:
            conn.execute(text(f"UPDATE {table} SET status = 'active' WHERE {key} = :u"), {"u": user_id(LENA)})


def test_a_cancelled_report_leaves_nothing_behind(login):
    import threading

    from abteilo.ai.orchestrator import Workspace
    from abteilo.db import transaction
    from abteilo.deps import load_department
    from abteilo.reports import generate
    from abteilo.security import login as sign_in, resolve_session
    from tests.conftest import PASSWORD

    eng = department_id(login(LENA), "Engineering")
    p = resolve_session(sign_in(LENA, PASSWORD).token)
    with transaction(p.company_id, p.user_id) as conn:
        ws = Workspace(load_department(conn, eng, "assistant"))
    scripted.replies.clear()
    scripted.replies.append(json.dumps(NARRATIVE))
    cancel, events = threading.Event(), []
    for event, _ in generate(p, ws, "Create a progress report", "pdf", "general", None, cancel):
        events.append(event)
        if event == "stage" and len(events) > 2:     # the client goes away while the model writes
            cancel.set()
    assert "result" not in events
    with owner_conn(p.user_id) as conn:
        assert conn.execute(text("SELECT count(*) FROM conv.reports")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM conv.messages")).scalar() == 0
        run = conn.execute(text("SELECT stage, status, error_code FROM core.ai_runs ORDER BY created_at DESC LIMIT 1")).one()
    assert tuple(run) == ("report", "error", "CLIENT_CANCELLED")


def test_migration_0009_reports_table_permissions_and_upgrade_path(login, demo):
    from tests.conftest import RUNTIME_URL, _alembic

    role = RUNTIME_URL.split("://", 1)[1].split(":", 1)[0]
    with owner_conn() as conn:
        grants = set(conn.execute(text("SELECT privilege_type FROM information_schema.role_table_grants WHERE"
                                       " table_schema = 'conv' AND table_name = 'reports' AND grantee = :r"), {"r": role}).scalars())
        forced = conn.execute(text("SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = 'conv.reports'::regclass")).scalar()
        policy = conn.execute(text("SELECT qual FROM pg_policies WHERE schemaname = 'conv' AND tablename = 'reports'")).scalar()
    assert grants == {"SELECT", "INSERT", "DELETE"} and forced            # immutable, row-level security forced
    assert "current_company_id()" in policy and "current_user_id()" in policy

    lena = login(LENA)                                                     # upgrade 0008 -> 0009 with live data
    _generate(lena, department_id(lena, "Engineering"), "Create a project progress report", NARRATIVE)
    with owner_conn() as conn:
        projects = conn.execute(text("SELECT count(*) FROM core.projects")).scalar()
    try:
        failed = False
        try:
            _alembic("downgrade", "0008")                                  # refused: report-stage usage history exists
        except Exception as exc:  # noqa: BLE001 - alembic wraps the database error
            failed = "report-stage runs" in str(exc)
        assert failed
        with owner_conn() as conn:
            conn.execute(text("DELETE FROM core.ai_runs WHERE stage = 'report'"))   # test-database cleanup only
        _alembic("downgrade", "0008")
        with owner_conn() as conn:
            assert conn.execute(text("SELECT to_regclass('conv.reports')")).scalar() is None
    finally:
        _alembic("upgrade")
    with owner_conn() as conn:
        assert conn.execute(text("SELECT count(*) FROM core.projects")).scalar() == projects   # business data untouched
        assert conn.execute(text("SELECT to_regclass('conv.reports')")).scalar() is not None


def test_reports_checked_by_older_rules_say_so():
    from abteilo.ai.evidence import VERIFIER_VERSION

    base = dict(kind="engineering_progress", title="Engineering progress report", company="BINA GmbH", scope="Engineering",
                author="Lena Weber", request="Create a report", generated_at=datetime.now(UTC),
                data_as_of=datetime.now(UTC), period="Current state", complete=True, summary="3 projects.",
                summary_basis="records")
    old = _docx_text(render_docx(Report(**base)))                                  # stored before versions existed
    assert "checked with an earlier version of the verification rules" in old
    current = _docx_text(render_docx(Report(**base, verifier_version=VERIFIER_VERSION)))
    assert "verification rules" not in current


def test_report_writing_emits_events_so_a_disconnect_is_noticed(login):
    """The HTTP layer learns of a disconnect only when it sends: report writing must not be silent until the end
    (browser acceptance 2026-10-23: Stop during writing was not noticed until the model had finished)."""
    lena = login(LENA)
    scripted.replies.clear()
    scripted.replies.append(json.dumps(NARRATIVE))
    stream = lena.post("/api/assistant/messages/stream",
                       json={"department_id": department_id(lena, "Engineering"), "message": "Create a progress report"})
    events = [block.split("\n", 1)[0].removeprefix("event: ") for block in stream.text.strip().split("\n\n")]
    writing, checking = events.index("stage"), len(events) - 1 - events[::-1].index("stage")
    assert "progress" in events[writing:checking] and events[-1] == "final"
