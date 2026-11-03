"""Typed, permission-checked tools the assistant may request.

The model proposes a tool name and arguments; this module validates them,
checks the principal's permission for the target, runs deterministic code and
returns evidence. The model never executes SQL or reaches data directly.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import Connection, text

from abteilo.ai.evidence import EdgeFact
from abteilo.authz import Principal
from abteilo.errors import AppError
from abteilo.modules import documents, engineering, finance, hr, it

MAX_EVIDENCE_CHARS = 1500


@dataclass(frozen=True)
class Evidence:
    kind: str  # document | record | aggregate | relationship; also the evidence authority
    title: str
    source_type: str
    source_id: str | None
    content: str
    as_of: datetime | None = None  # document version date; live records are stamped at query time
    freshness: str = "live"        # live | current_version | projection | projection_stale
    edges: tuple[EdgeFact, ...] = ()  # relationship evidence: the edges behind `content`, for the verifier


class ToolDegraded(Exception):
    """A source could not answer within its bounds. The request continues; the code is reported."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ToolContext:
    conn: Connection
    principal: Principal
    department_id: UUID | None
    question: str


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: Annotated[str, StringConstraints(min_length=2, max_length=300)]


class ProjectArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_code: Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9-]{1,19}$")]


class PeriodArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    period: Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-Q[1-4]$")] | None = None


class RelatedArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: Annotated[str, StringConstraints(min_length=2, max_length=120)]
    depth: Literal[1, 2] = 2


class StatusArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["submitted", "approved", "rejected", "cancelled", "proposed"] | None = None


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: type[BaseModel]
    allowed: Callable[[Principal, UUID | None], bool]
    run: Callable[[ToolContext, BaseModel], list[Evidence]]
    module: str | None = None  # department module that must be switched on; None = always available


# Tools that read a department module's data are offered only while that module is on.
_TOOL_MODULES = {
    "documents.search": "documents",
    "engineering.list_projects": "projects", "engineering.get_project": "projects",
    "finance.budget_summary": "finance", "finance.list_purchase_requests": "finance",
    "hr.pending_leave": "hr",
    "it.list_access_requests": "it", "it.role_catalog": "it",
}


def _person(name: str | None, active: bool | None, missing: str = 'unassigned') -> str:
    """INT-08 for record evidence: the assistant never names a suspended or disabled member. The decision uses the
    typed `*_active` field the module returns from live membership, not name matching."""
    if name is None:
        return missing
    return name if active is not False else 'an inactive member'


def _money(cents: int) -> str:
    return f"{cents / 100:.2f} EUR"


def _search(ctx: ToolContext, args: SearchArgs) -> list[Evidence]:
    hits = documents.search_documents(ctx.conn, ctx.principal, args.query, ctx.department_id)
    return [
        Evidence("document", f"{h.document_title} (v{h.version}, part {h.chunk_index + 1})", "document",
                 str(h.document_id), h.content[:MAX_EVIDENCE_CHARS], h.version_created_at, freshness="current_version")
        for h in hits
    ]


def _related(ctx: ToolContext, args: RelatedArgs) -> list[Evidence]:
    from abteilo.intelligence import graph_source

    try:
        result = graph_source.related(ctx.conn, ctx.principal, args.subject, args.depth)
    except graph_source.GraphDegraded as exc:
        raise ToolDegraded(exc.code) from exc
    if result is None:
        return []
    return [Evidence("relationship", f"Relationships of {result.root_label}", "graph_relationships", None, result.content,
                     result.projected_at, freshness="projection_stale" if result.stale else "projection", edges=result.edges)]


def _list_projects(ctx: ToolContext, _: NoArgs) -> list[Evidence]:
    projects = engineering.list_projects(ctx.conn, ctx.principal, ctx.department_id)
    blocked = ctx.conn.execute(
        text("SELECT p.code, t.title FROM core.tasks t JOIN core.projects p ON p.id = t.project_id"
             " WHERE p.department_id = :d AND t.status = 'blocked' ORDER BY p.code LIMIT 50"),
        {"d": ctx.department_id},
    ).all()
    lines = [f"{p.code} | {p.name} | status={p.status} | open_tasks={p.open_tasks} | blocked_tasks={p.blocked_tasks}"
             for p in projects]
    lines += [f"blocked task in {code}: {title}" for code, title in blocked]
    return [Evidence("record", "Engineering projects", "project_list", None, "\n".join(lines) or "No projects.")]


def _get_project(ctx: ToolContext, args: ProjectArgs) -> list[Evidence]:
    try:
        project, tasks = engineering.get_project(ctx.conn, ctx.principal, args.project_code)
    except AppError:
        return []
    task_lines = [f"- {t.title} | status={t.status} | assignee={_person(t.assignee_name, t.assignee_active)}"
                  f" | due={t.due_date or 'none'}" for t in tasks[:30]]
    # One line per fact-bearing entity, so a status is checked against the right subject.
    content = (f"Project {project.code} | {project.name} | status={project.status} | updated={project.updated_at:%Y-%m-%d}\n"
               f"description: {project.description}\ntasks of {project.code}:\n" + "\n".join(task_lines))
    return [Evidence("record", f"Project {project.code}", "project", str(project.id), content[:MAX_EVIDENCE_CHARS])]


def _budget_summary(ctx: ToolContext, args: PeriodArgs) -> list[Evidence]:
    lines = [f"{b.department_name} | {b.period} | budget={_money(b.budget_cents)} | approved={_money(b.approved_cents)}"
             f" | pending={_money(b.pending_cents)} | remaining={_money(b.remaining_cents)}"
             for b in finance.budget_summary(ctx.conn, ctx.principal, ctx.department_id, args.period)]
    return [Evidence("record", "Budget summary", "budget_summary", None, "\n".join(lines) or "No budgets recorded.")]


def _purchase_requests(ctx: ToolContext, args: StatusArgs) -> list[Evidence]:
    status = args.status if args.status in ("submitted", "approved", "rejected", "cancelled") else None
    rows = finance.list_purchase_requests(ctx.conn, ctx.principal, ctx.department_id, status)[:25]
    lines = [f"{r.title} | {r.department_name} | {_money(r.amount_cents)} | {r.period} | status={r.status}"
             f" | requested_by={_person(r.requester_name, r.requester_active)}" for r in rows]
    return [Evidence("record", "Purchase requests", "purchase_requests", None, "\n".join(lines) or "No purchase requests.")]


def _my_leave(ctx: ToolContext, _: NoArgs) -> list[Evidence]:
    s = hr.leave_summary(ctx.conn, ctx.principal.user_id, date.today().year)
    lines = [f"year={s.year} total_days={s.days_total} approved_days={s.days_approved} pending_days={s.days_pending}"
             f" remaining_days={s.days_remaining}"]
    lines += [f"- {r.start_date} to {r.end_date} | {r.days} days | status={r.status}" for r in s.requests[:20]]
    return [Evidence("record", "Your leave balance", "leave_summary", None, "\n".join(lines))]


def _pending_leave(ctx: ToolContext, _: NoArgs) -> list[Evidence]:
    rows = hr.pending_leave(ctx.conn, ctx.principal, ctx.department_id)[:25]
    lines = [f"{_person(r.employee_name, r.employee_active)} | {r.start_date} to {r.end_date} | {r.days} days"
             f" | status={r.status}" for r in rows]
    return [Evidence("record", "Pending leave requests", "leave_requests", None, "\n".join(lines) or "None pending.")]


def _access_requests(ctx: ToolContext, args: StatusArgs) -> list[Evidence]:
    status = args.status if args.status in ("proposed", "approved", "rejected") else None
    rows = it.list_access_requests(ctx.conn, ctx.principal, ctx.department_id, status)[:25]
    lines = [f"{_person(r.target_name, r.target_active)} | role={r.role_code} | dept={r.department_name or 'company'} | status={r.status}"
             f" | proposed_by={_person(r.proposer_name, r.proposer_active)}" for r in rows]
    return [Evidence("record", "Access requests", "access_requests", None, "\n".join(lines) or "No access requests.")]


def _role_catalog(ctx: ToolContext, _: NoArgs) -> list[Evidence]:
    lines = [f"{r.code} ({r.scope}): {', '.join(r.permissions)}" for r in it.role_catalog(ctx.conn)]
    return [Evidence("record", "Role catalog", "roles", None, "\n".join(lines)[:MAX_EVIDENCE_CHARS])]


def _company_overview(ctx: ToolContext, _: NoArgs) -> list[Evidence]:
    """Same deterministic data as the owner Overview page; one fact per line."""
    from abteilo.overview import build_overview

    o = build_overview(ctx.conn, ctx.principal)
    pend = o.pending
    lines = [
        f"Period {o.period} | active_members={o.active_members} | active_departments={o.active_departments}",
        f"Pending approvals | purchase_requests={pend.purchase_requests} ({_money(pend.purchase_amount_cents)})"
        f" | leave_requests={pend.leave_requests} | access_requests={pend.access_requests}"
        f" | elevated_access_requests={pend.elevated_access_requests}",
    ]
    lines += [f"Project {b.code} | {b.name} | {b.department_name} | status=blocked"
              f" | blocked tasks: {', '.join(b.blocked_tasks) or 'none recorded'}" for b in o.blocked_projects]
    lines += [f"Budget pressure {b.department_name} | {b.period} | budget={_money(b.budget_cents)}"
              f" | approved={_money(b.approved_cents)} | pending={_money(b.pending_cents)}" for b in o.budget_pressure]
    lines += [f"Department {d.name} | members={d.members} | active_projects={d.active_projects}"
              f" | blocked_projects={d.blocked_projects} | pending_purchases={d.pending_purchases} | documents={d.documents}"
              for d in o.departments]
    return [Evidence("aggregate", "Company overview", "company_overview", None, "\n".join(lines)[:MAX_EVIDENCE_CHARS])]


def _department_activity(ctx: ToolContext, _: NoArgs) -> list[Evidence]:
    rows = ctx.conn.execute(text(
        "SELECT action, count(*) AS n FROM core.audit_events WHERE occurred_at > now() - interval '24 hours'"
        " AND action NOT LIKE 'auth.%' GROUP BY action ORDER BY n DESC LIMIT 30"
    )).all()
    lines = [f"{r.action}: {r.n}" for r in rows]
    return [Evidence("aggregate", "Business activity in the last 24 hours", "activity", None,
                     "\n".join(lines) or "No business activity recorded in the last 24 hours.")]


def _in_dept(perm: str) -> Callable[[Principal, UUID | None], bool]:
    return lambda p, d: d is not None and p.can(perm, d)


TOOLS: dict[str, Tool] = {
    t.name: Tool(t.name, t.description, t.args, t.allowed, t.run, _TOOL_MODULES.get(t.name))
    for t in (
        Tool("documents.search", "Full-text search of documents you may read. Use for policies, specifications, procedures.",
             SearchArgs, lambda p, d: p.can("documents.read", d) or d is None, _search),
        Tool("graph.related", "Records connected to ONE named record (subject = a project code such as A17, or the exact"
             " name of a task, request, document or person): what it depends on, what blocks it, requests made for it,"
             " documents mentioning it. Not for lists or counts; current statuses are re-read from live records.",
             RelatedArgs, lambda p, d: True, _related),
        Tool("engineering.list_projects", "List engineering projects with status and task counts.", NoArgs,
             _in_dept("engineering.project.read"), _list_projects),
        Tool("engineering.get_project", "Get one project with its tasks, by project code such as A17.", ProjectArgs,
             _in_dept("engineering.project.read"), _get_project),
        Tool("finance.budget_summary", "Department budgets with approved, pending and remaining amounts for a period like 2026-Q4.",
             PeriodArgs, _in_dept("finance.budget.read"), _budget_summary),
        Tool("finance.list_purchase_requests", "List purchase requests, optionally filtered by status (open or pending = submitted).", StatusArgs,
             _in_dept("finance.budget.read"), _purchase_requests),
        Tool("hr.my_leave", "The current user's own leave balance and requests.", NoArgs,
             lambda p, d: p.can("leave.request.create"), _my_leave),
        Tool("hr.pending_leave", "Leave requests waiting for an HR decision.", NoArgs,
             _in_dept("hr.leave.approve"), _pending_leave),
        Tool("it.list_access_requests", "Role-change requests, optionally filtered by status (open or pending = proposed).", StatusArgs,
             lambda p, d: d is not None and it.can_view_access(p, d), _access_requests),
        Tool("it.role_catalog", "All roles and the permissions each grants.", NoArgs,
             lambda p, d: d is not None and it.can_view_access(p, d), _role_catalog),
        Tool("company.overview", "Company-wide figures: blocked projects with their blocked tasks, pending approvals"
             " (purchase, leave and access requests), budget pressure, and per-department projects, purchases and"
             " documents. Use it for counts, blocked work and anything waiting for a decision.", NoArgs,
             lambda p, d: p.has_company_wide("report.executive.read"), _company_overview),
        Tool("company.department_activity", "Counts of business actions in the last 24 hours.", NoArgs,
             lambda p, d: p.has_company_wide("report.executive.read"), _department_activity),
    )
}
