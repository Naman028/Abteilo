"""Describe the evaluation seed for an independent case author, as data only.

    python -m eval.sealed.seed_facts > SEED_FACTS.md

Reads the disposable test database after `eval.intelligence` has seeded it (any L2 run leaves it seeded) and
lists who exists, who may do what, and every record, document and reviewed relationship. It contains no
questions, no expected answers and nothing about how the system decides.
"""

import os
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from eval.intelligence import _env_from_dotenv  # noqa: E402


def main() -> None:
    _env_from_dotenv()
    from abteilo.db import configure_engine, transaction
    configure_engine(os.environ["ABTEILO_DATABASE_URL"])

    out = ["# Evaluation seed: facts for an independent case author", "",
           "Fictional data. Users sign in with their e-mail; the case `user` field is the part before `@`.", ""]
    with transaction() as conn:
        companies = conn.execute(text("SELECT DISTINCT m.company_id FROM core.memberships m")).scalars().all()
    for company in companies:
        with transaction(company) as conn:
            q = lambda sql: conn.execute(text(sql)).all()  # noqa: E731
            name = conn.execute(text("SELECT name FROM core.companies WHERE id = core.current_company_id()")).scalar()
            out += [f"## {name}", "", "### People and roles", "",
                    "| Person | E-mail | Membership | Roles |", "|---|---|---|---|"]
            for r in q("SELECT u.display_name, u.email, m.status, coalesce(string_agg(ra.role_code ||"
                       " coalesce(' @ ' || d.name, ''), ', ' ORDER BY ra.role_code), '') FROM core.memberships m"
                       " JOIN core.users u ON u.id = m.user_id LEFT JOIN core.role_assignments ra ON ra.user_id = m.user_id"
                       " AND ra.company_id = m.company_id AND ra.revoked_at IS NULL LEFT JOIN core.departments d"
                       " ON d.id = ra.department_id WHERE m.company_id = core.current_company_id()"
                       " GROUP BY 1, 2, 3 ORDER BY 1"):
                out.append(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} |")
            out += ["", "### Projects and tasks", "", "| Project | Status | Task | Task status | Assignee | Due |",
                    "|---|---|---|---|---|---|"]
            for r in q("SELECT p.code || ' ' || p.name, p.status, t.title, t.status, u.display_name, t.due_date"
                       " FROM core.projects p LEFT JOIN core.tasks t ON t.project_id = p.id LEFT JOIN core.users u"
                       " ON u.id = t.assignee_id ORDER BY 1, 3"):
                out.append("| " + " | ".join("" if v is None else str(v) for v in r) + " |")
            out += ["", "### Budgets and purchase requests", ""]
            out += [f"- Budget {r[0]} {r[1]}: {r[2] / 100:.2f} EUR" for r in
                    q("SELECT d.name, b.period, b.amount_cents FROM core.budgets b JOIN core.departments d"
                      " ON d.id = b.department_id ORDER BY 1")]
            out += [f"- Purchase request '{r[0]}' ({r[1]}, {r[2] / 100:.2f} EUR, {r[3]}): {r[4]}, requested by {r[5]}"
                    for r in q("SELECT pr.title, d.name, pr.amount_cents, pr.period, pr.status, u.display_name"
                               " FROM core.purchase_requests pr JOIN core.departments d ON d.id = pr.department_id"
                               " JOIN core.users u ON u.id = pr.requested_by ORDER BY 1")]
            out += ["", "### Leave and access requests", ""]
            out += [f"- Leave {r[0]}: {r[1]} to {r[2]}, {r[3]} days, {r[4]}" for r in
                    q("SELECT u.display_name, lr.start_date, lr.end_date, lr.days, lr.status FROM core.leave_requests lr"
                      " JOIN core.users u ON u.id = lr.user_id ORDER BY 2")]
            out += [f"- Access request: role {r[0]} for {r[1]} ({r[2] or 'company'}), {r[3]}, proposed by {r[4]}" for r in
                    q("SELECT ar.role_code, t.display_name, d.name, ar.status, p.display_name FROM core.access_requests ar"
                      " JOIN core.users t ON t.id = ar.target_user_id JOIN core.users p ON p.id = ar.proposed_by"
                      " LEFT JOIN core.departments d ON d.id = ar.department_id ORDER BY 1")]
            out += ["", "### Documents (current version)", ""]
            for r in q("SELECT d.title, dep.name, d.classification, d.current_version, v.content FROM core.documents d"
                       " JOIN core.departments dep ON dep.id = d.department_id JOIN core.document_versions v"
                       " ON v.document_id = d.id AND v.version = d.current_version ORDER BY 2, 1"):
                out += [f"**{r[0]}** ({r[1]}, {r[2]}, version {r[3]})", "", "> " + r[4].replace("\n", "\n> "), ""]
            out += ["### Reviewed relationships", ""]
            out += [f"- {r[0]} — {r[1]} → {r[2]}" for r in
                    q("SELECT s.label, gr.kind, d.label FROM core.graph_relationships gr JOIN core.graph_entities s"
                      " ON s.id = gr.src_id JOIN core.graph_entities d ON d.id = gr.dst_id"
                      " WHERE gr.authority = 'reviewed' AND gr.valid_to IS NULL ORDER BY 1")]
            out.append("")
    print("\n".join(out))


if __name__ == "__main__":
    main()
