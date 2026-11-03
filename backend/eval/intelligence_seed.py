"""Seed for the Block D evaluation (eval/intelligence.py): the demo company plus the situations Block D must
handle. Everything is fictional. Ground truth in intelligence_cases.json is written from these definitions,
independently of the code under test.

Added on top of `abteilo.seed`:
- a reviewed blocking edge: "Thermal validation retest" blocked_by "Order replacement cooling module" (a cause);
- a reviewed dependency: "Connect line 2 PLC data" depends_on "Downtime alert thresholds" (not, by itself, a cause);
- a document that contradicts a live record: "B24 Weekly Status" says B24 is blocked (the record says active);
- a superseded policy: the Leave Policy v2 moves the carry-over deadline from 31 March to 30 April;
- a suspended engineer, Paul Wagner, still assigned to the B24 task "Downtime alert thresholds";
- a second company, Nordwerk AG, with project N01 and employee Kai Vogt; Jonas belongs to both.
"""

from uuid import UUID, uuid4

from sqlalchemy import text

from abteilo import company
from abteilo.admin import set_member_status
from abteilo.authz import Principal, load_grants
from abteilo.db import transaction
from abteilo.graph_projection import project_company
from abteilo.modules.documents import add_version, store_document
from abteilo.seed import seed
from abteilo.templates import MAJOR_DEPARTMENTS

CONTRADICTION = ("B24 Weekly Status", "Project B24 is blocked: the team is waiting for PLC access on line 2.\n\n"
                 "The PLC data connection depends on the downtime alert thresholds.\n\nNext review on Friday.")
DEPENDENCY_QUOTE = "The PLC data connection depends on the downtime alert thresholds."
LEAVE_V2 = ("Full-time employees receive 30 days of annual leave per calendar year.\n\nLeave requests are submitted in "
            "Abteilo and decided by HR. Unused leave can be carried over until 30 April of the following year.")
CAUSAL_QUOTE = "A replacement cooling module and a new test rig are required before the retest."


def _principal(conn, company_id: UUID, user_id: UUID) -> Principal:
    return Principal(user_id, company_id, "", "", load_grants(conn, company_id, user_id))


def build(password: str) -> UUID:
    cid = seed(password)
    with transaction(cid) as conn:
        users = dict(conn.execute(text("SELECT split_part(u.email, '@', 1), u.id FROM core.users u JOIN core.memberships m"
                                       " ON m.user_id = u.id WHERE m.company_id = :c"), {"c": cid}).all())
        depts = dict(conn.execute(text("SELECT template, id FROM core.departments")).all())
    lena, mila, owner = users["lena.weber"], users["mila.hoffmann"], users["owner"]

    with transaction(cid, owner) as conn:
        paul = company.add_member(conn, cid, owner, email="paul.wagner@bina.example", display_name="Paul Wagner",
                                  password=password, title="Automation Engineer")
        company.assign_role(conn, cid, paul, "engineering_contributor", depts["engineering"], owner)
        conn.execute(text("UPDATE core.tasks SET assignee_id = :u WHERE title = 'Downtime alert thresholds'"), {"u": paul})
    with transaction(cid, lena) as conn:
        store_document(conn, _principal(conn, cid, lena), depts["engineering"], CONTRADICTION[0], "department", CONTRADICTION[1])
    with transaction(cid, mila) as conn:
        leave = conn.execute(text("SELECT id FROM core.documents WHERE title = 'Leave Policy'")).scalar_one()
        add_version(conn, _principal(conn, cid, mila), leave, LEAVE_V2)
    with transaction(cid, owner) as conn:
        set_member_status(conn, paul, "disabled")        # suspended after the assignment, as in real life

    project_company(cid)
    with transaction(cid) as conn:   # reviewed relationships (as if an editor accepted the suggestions)
        tasks = dict(conn.execute(text("SELECT label, id FROM core.graph_entities WHERE kind = 'task'")).all())
        for kind, src, dst, title, quote in (
            ("blocked_by", "Thermal validation retest", "Order replacement cooling module",
             "A17 Thermal Validation Report", CAUSAL_QUOTE),
            ("depends_on", "Connect line 2 PLC data", "Downtime alert thresholds", CONTRADICTION[0], DEPENDENCY_QUOTE),
        ):
            doc, version = conn.execute(text(
                "SELECT d.id, v.id FROM core.documents d JOIN core.document_versions v ON v.document_id = d.id"
                " AND v.version = d.current_version WHERE d.title = :t"), {"t": title}).one()
            rel = uuid4()
            conn.execute(text("INSERT INTO core.graph_relationships (id, company_id, src_id, dst_id, kind, authority,"
                              " document_id) VALUES (:id, :c, :s, :d, :k, 'reviewed', :doc)"),
                         {"id": rel, "c": cid, "s": tasks[src], "d": tasks[dst], "k": kind, "doc": doc})
            conn.execute(text("INSERT INTO core.graph_evidence (id, company_id, relationship_id, source_type, source_id,"
                              " document_version_id, quote) VALUES (:id, :c, :r, 'document_version', :v, :v, :q)"),
                         {"id": uuid4(), "c": cid, "r": rel, "v": version, "q": quote})

    nord = company.create_company(name="Nordwerk AG (eval)", slug="nordwerk", industry="manufacturing",
                                  owner_email="owner@nordwerk.example", owner_name="Nord Owner", owner_password=password,
                                  department_templates=list(MAJOR_DEPARTMENTS))
    with transaction(nord.company_id, nord.owner_user_id) as conn:
        eng = nord.department_ids["engineering"]
        kai = company.add_member(conn, nord.company_id, nord.owner_user_id, email="kai.vogt@nordwerk.example",
                                 display_name="Kai Vogt", password=password, title="Test Engineer")
        company.assign_role(conn, nord.company_id, kai, "engineering_lead", eng, nord.owner_user_id)
        # Joined later than BINA, so Jonas's sign-in (first membership by join date) is always BINA.
        conn.execute(text("INSERT INTO core.memberships (company_id, user_id, title, joined_at)"
                          " VALUES (:c, :u, 'Consultant', now() + interval '1 minute')"),
                     {"c": nord.company_id, "u": users["jonas.becker"]})
        company.assign_role(conn, nord.company_id, users["jonas.becker"], "employee", None, nord.owner_user_id)
        project = uuid4()
        conn.execute(text("INSERT INTO core.projects (id, company_id, department_id, code, name, status, created_by)"
                          " VALUES (:id, :c, :d, 'N01', 'Gearbox Endurance', 'blocked', :u)"),
                     {"id": project, "c": nord.company_id, "d": eng, "u": kai})
        conn.execute(text("INSERT INTO core.tasks (id, company_id, project_id, title, status, assignee_id, created_by)"
                          " VALUES (:id, :c, :p, 'Gearbox endurance run', 'blocked', :a, :u)"),
                     {"id": uuid4(), "c": nord.company_id, "p": project, "a": kai, "u": kai})
        store_document(conn, _principal(conn, nord.company_id, kai), eng, "N01 Gearbox Test Plan", "department",
                       "Project N01 gearbox endurance test plan. Kai Vogt leads it. The CFO of Nordwerk is Dr. Ines Roth.")
    project_company(nord.company_id)
    return cid
