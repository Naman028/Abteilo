"""Declarative seed extension for the sealed evaluation.

The independent author may need situations the development seed lacks (overlapping labels, revoked roles, a
misleading policy). They describe them in JSON; this module validates and applies the file on top of
`eval.intelligence_seed.build`, so the system's developer never writes the sealed data or the questions.

Schema (all lists optional):

    {
      "projects":  [{"code": "A18", "name": "...", "status": "active|planned|blocked|done", "department": "engineering"}],
      "tasks":     [{"project": "A18", "title": "...", "status": "todo|in_progress|blocked|done",
                     "assignee": "jonas.becker" | null, "due": "2026-12-01" | null}],
      "documents": [{"department": "finance", "title": "...", "classification": "company|department|confidential",
                     "author": "sara.koch", "content": "..."}],
      "purchase_requests": [{"title": "...", "department": "engineering", "project": "A17" | null,
                             "amount_eur": 1200.00, "period": "2026-Q4", "requester": "jonas.becker",
                             "justification": "..."}],
      "revoke_roles": [{"user": "tim.schulz", "role": "it_access_admin", "department": "it_security"}],
      "suspend":   ["sara.koch"]
    }

Users are the local part of a BINA e-mail (`jonas.becker`); departments are templates (`engineering`,
`finance`, `human_resources`, `it_security`). Everything runs through the same functions and permission checks
as the application; nothing bypasses row-level security.
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

Department = Literal["engineering", "finance", "human_resources", "it_security"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectSpec(_Strict):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9-]{1,19}$")
    name: str = Field(min_length=2, max_length=120)
    status: Literal["planned", "active", "blocked", "done"] = "active"
    department: Department = "engineering"


class TaskSpec(_Strict):
    project: str
    title: str = Field(min_length=2, max_length=200)
    status: Literal["todo", "in_progress", "blocked", "done"] = "todo"
    assignee: str | None = None
    due: date | None = None


class DocumentSpec(_Strict):
    department: Department
    title: str = Field(min_length=2, max_length=200)
    classification: Literal["company", "department", "confidential"] = "department"
    author: str
    content: str = Field(min_length=1, max_length=20000)


class PurchaseSpec(_Strict):
    title: str = Field(min_length=2, max_length=200)
    department: Department
    project: str | None = None
    amount_eur: Decimal = Field(gt=0)
    period: str = Field(pattern=r"^[0-9]{4}-Q[1-4]$")
    requester: str
    justification: str = Field(min_length=1, max_length=2000)


class RevokeSpec(_Strict):
    user: str
    role: str
    department: Department | None = None


class Extension(_Strict):
    projects: list[ProjectSpec] = []
    tasks: list[TaskSpec] = []
    documents: list[DocumentSpec] = []
    purchase_requests: list[PurchaseSpec] = []
    revoke_roles: list[RevokeSpec] = []
    suspend: list[str] = []


def load(path: str | Path) -> Extension:
    return Extension.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def apply(company_id: UUID, ext: Extension) -> None:
    from abteilo.admin import set_member_status
    from abteilo.db import transaction
    from abteilo.modules.documents import store_document
    from eval.intelligence_seed import _principal

    with transaction(company_id) as conn:
        users = dict(conn.execute(text("SELECT split_part(u.email, '@', 1), u.id FROM core.users u JOIN core.memberships m"
                                       " ON m.user_id = u.id WHERE m.company_id = :c"), {"c": company_id}).all())
        depts = dict(conn.execute(text("SELECT template, id FROM core.departments")).all())
    owner = users["owner"]

    def user(name: str) -> UUID:
        if name not in users:
            raise ValueError(f"unknown user {name!r}")
        return users[name]

    with transaction(company_id, owner) as conn:
        for p in ext.projects:
            conn.execute(text("INSERT INTO core.projects (id, company_id, department_id, code, name, status, created_by)"
                              " VALUES (:id, :c, :d, :code, :n, :s, :u)"),
                         {"id": uuid4(), "c": company_id, "d": depts[p.department], "code": p.code, "n": p.name,
                          "s": p.status, "u": owner})
        for t in ext.tasks:
            project = conn.execute(text("SELECT id FROM core.projects WHERE code = :c"), {"c": t.project}).scalar_one()
            conn.execute(text("INSERT INTO core.tasks (id, company_id, project_id, title, status, assignee_id, due_date,"
                              " created_by) VALUES (:id, :c, :p, :t, :s, :a, :due, :u)"),
                         {"id": uuid4(), "c": company_id, "p": project, "t": t.title, "s": t.status,
                          "a": user(t.assignee) if t.assignee else None, "due": t.due, "u": owner})
        for r in ext.purchase_requests:
            project = (conn.execute(text("SELECT id FROM core.projects WHERE code = :c"), {"c": r.project}).scalar_one()
                       if r.project else None)
            conn.execute(text("INSERT INTO core.purchase_requests (id, company_id, department_id, project_id, title,"
                              " justification, amount_cents, period, requested_by) VALUES (:id, :c, :d, :p, :t, :j, :a,"
                              " :per, :u)"),
                         {"id": uuid4(), "c": company_id, "d": depts[r.department], "p": project, "t": r.title,
                          "j": r.justification, "a": int(r.amount_eur * 100), "per": r.period, "u": user(r.requester)})
    for d in ext.documents:
        author = user(d.author)
        with transaction(company_id, author) as conn:
            store_document(conn, _principal(conn, company_id, author), depts[d.department], d.title, d.classification,
                           d.content)
    with transaction(company_id, owner) as conn:
        for r in ext.revoke_roles:
            conn.execute(text("UPDATE core.role_assignments SET revoked_at = now(), revoked_by = :o WHERE user_id = :u"
                              " AND role_code = :r AND revoked_at IS NULL"
                              " AND (CAST(:d AS uuid) IS NULL OR department_id = :d)"),
                         {"o": owner, "u": user(r.user), "r": r.role, "d": depts[r.department] if r.department else None})
        for name in ext.suspend:
            set_member_status(conn, user(name), "disabled")
