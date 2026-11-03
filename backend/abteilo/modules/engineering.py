"""Engineering: projects and tasks."""

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends
from pydantic import BaseModel, StringConstraints
from sqlalchemy import Connection, text

from abteilo import audit
from abteilo.authz import Principal
from abteilo.db import set_clause
from abteilo.deps import current_principal, load_department, tx
from abteilo.errors import Invalid, NotFound

router = APIRouter(prefix="/api/engineering", tags=["engineering"])

ProjectStatus = Literal["planned", "active", "blocked", "done", "archived"]
TaskStatus = Literal["todo", "in_progress", "blocked", "done"]


class ProjectView(BaseModel):
    id: UUID
    department_id: UUID
    code: str
    name: str
    description: str
    status: ProjectStatus
    open_tasks: int
    blocked_tasks: int
    updated_at: datetime


class TaskView(BaseModel):
    id: UUID
    project_id: UUID
    title: str
    status: TaskStatus
    assignee_id: UUID | None
    assignee_name: str | None
    assignee_active: bool | None = None   # live membership; None when unassigned
    due_date: date | None
    updated_at: datetime


class CreateProject(BaseModel):
    code: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9-]{1,19}$")]
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=120)]
    description: Annotated[str, StringConstraints(max_length=4000)] = ""


class SetProjectStatus(BaseModel):
    status: ProjectStatus


class CreateTask(BaseModel):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=200)]
    assignee_id: UUID | None = None
    due_date: date | None = None


class UpdateTask(BaseModel):
    status: TaskStatus | None = None
    assignee_id: UUID | None = None
    due_date: date | None = None


_PROJECT_SQL = """
SELECT p.id, p.department_id, p.code, p.name, p.description, p.status, p.updated_at,
       count(t.id) FILTER (WHERE t.status <> 'done') AS open_tasks,
       count(t.id) FILTER (WHERE t.status = 'blocked') AS blocked_tasks
FROM core.projects p LEFT JOIN core.tasks t ON t.company_id = p.company_id AND t.project_id = p.id
WHERE {where}
GROUP BY p.id ORDER BY p.code LIMIT 200
"""


def list_projects(conn: Connection, p: Principal, department_id: UUID) -> list[ProjectView]:
    p.require("engineering.project.read", department_id)
    rows = conn.execute(text(_PROJECT_SQL.format(where="p.department_id = :d")), {"d": department_id}).mappings()
    return [ProjectView(**r) for r in rows]


def get_project(conn: Connection, p: Principal, project_ref: str | UUID) -> tuple[ProjectView, list[TaskView]]:
    """Look up by id or by code; permission is checked against the project's own department."""
    where, params = ("p.id = :ref", {"ref": project_ref}) if isinstance(project_ref, UUID) else ("p.code = :ref", {"ref": project_ref.upper()})
    row = conn.execute(text(_PROJECT_SQL.format(where=where)), params).mappings().first()
    if row is None or not p.can("engineering.project.read", row["department_id"]):
        raise NotFound()
    project = ProjectView(**row)
    tasks = conn.execute(
        text(
            "SELECT t.id, t.project_id, t.title, t.status, t.assignee_id, u.display_name AS assignee_name,"
            " CASE WHEN t.assignee_id IS NULL THEN NULL ELSE EXISTS (SELECT 1 FROM core.memberships am JOIN core.users au ON au.id = am.user_id WHERE am.company_id = core.current_company_id() AND am.user_id = t.assignee_id AND am.status = 'active' AND au.status = 'active') END AS assignee_active,"
            " t.due_date, t.updated_at FROM core.tasks t LEFT JOIN core.users u ON u.id = t.assignee_id"
            " WHERE t.project_id = :p ORDER BY t.status, t.created_at LIMIT 500"
        ),
        {"p": project.id},
    ).mappings()
    return project, [TaskView(**t) for t in tasks]


def _project_department(conn: Connection, project_id: UUID) -> UUID:
    dept = conn.execute(text("SELECT department_id FROM core.projects WHERE id = :p"), {"p": project_id}).scalar()
    if dept is None:
        raise NotFound()
    return dept


def _require_member(conn: Connection, user_id: UUID | None) -> None:
    if user_id is not None and not conn.execute(
        text("SELECT 1 FROM core.memberships WHERE company_id = core.current_company_id() AND user_id = :u AND status = 'active'"),
        {"u": user_id},
    ).first():
        raise Invalid("Assignee is not an active member.")


@router.get("/{department_id}/projects", response_model=list[ProjectView])
def api_list_projects(department_id: UUID, p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        load_department(conn, department_id, "projects")
        return list_projects(conn, p, department_id)


@router.post("/{department_id}/projects", response_model=ProjectView, status_code=201)
def api_create_project(department_id: UUID, body: CreateProject, p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        load_department(conn, department_id, "projects")
        p.require("engineering.project.manage", department_id)
        project_id = uuid4()
        conn.execute(
            text(
                "INSERT INTO core.projects (id, company_id, department_id, code, name, description, created_by)"
                " VALUES (:id, core.current_company_id(), :d, :code, :n, :desc, :u)"
                " ON CONFLICT (company_id, code) DO NOTHING"
            ),
            {"id": project_id, "d": department_id, "code": body.code, "n": body.name, "desc": body.description, "u": p.user_id},
        )
        if not conn.execute(text("SELECT 1 FROM core.projects WHERE id = :id"), {"id": project_id}).first():
            raise Invalid("A project with this code already exists.")
        audit.record(conn, p.company_id, p.user_id, "engineering.project_created", "project", project_id, {"code": body.code})
        return get_project(conn, p, project_id)[0]


@router.patch("/projects/{project_id}", response_model=ProjectView)
def api_set_project_status(project_id: UUID, body: SetProjectStatus, p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        p.require("engineering.project.manage", _project_department(conn, project_id))
        conn.execute(
            text("UPDATE core.projects SET status = :s, updated_at = now() WHERE id = :p"), {"s": body.status, "p": project_id}
        )
        audit.record(conn, p.company_id, p.user_id, "engineering.project_status_changed", "project", project_id, {"status": body.status})
        return get_project(conn, p, project_id)[0]


@router.get("/projects/{project_id}/tasks", response_model=list[TaskView])
def api_list_tasks(project_id: UUID, p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        return get_project(conn, p, project_id)[1]


@router.post("/projects/{project_id}/tasks", response_model=TaskView, status_code=201)
def api_create_task(project_id: UUID, body: CreateTask, p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        p.require("engineering.task.write", _project_department(conn, project_id))
        _require_member(conn, body.assignee_id)
        task_id = uuid4()
        conn.execute(
            text(
                "INSERT INTO core.tasks (id, company_id, project_id, title, assignee_id, due_date, created_by)"
                " VALUES (:id, core.current_company_id(), :p, :t, :a, :due, :u)"
            ),
            {"id": task_id, "p": project_id, "t": body.title, "a": body.assignee_id, "due": body.due_date, "u": p.user_id},
        )
        conn.execute(text("UPDATE core.projects SET updated_at = now() WHERE id = :p"), {"p": project_id})
        audit.record(conn, p.company_id, p.user_id, "engineering.task_created", "task", task_id)
        return next(t for t in get_project(conn, p, project_id)[1] if t.id == task_id)


@router.patch("/tasks/{task_id}", response_model=TaskView)
def api_update_task(task_id: UUID, body: UpdateTask, p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        project_id = conn.execute(text("SELECT project_id FROM core.tasks WHERE id = :t"), {"t": task_id}).scalar()
        if project_id is None:
            raise NotFound()
        p.require("engineering.task.write", _project_department(conn, project_id))
        changes = body.model_dump(exclude_unset=True)
        if "assignee_id" in changes:
            _require_member(conn, changes["assignee_id"])
        if changes:
            conn.execute(text(f"UPDATE core.tasks SET {set_clause(changes)}, updated_at = now() WHERE id = :t"),
                         {**changes, "t": task_id})
            conn.execute(text("UPDATE core.projects SET updated_at = now() WHERE id = :p"), {"p": project_id})
            audit.record(conn, p.company_id, p.user_id, "engineering.task_updated", "task", task_id, {"fields": sorted(changes)})
        return next(t for t in get_project(conn, p, project_id)[1] if t.id == task_id)
