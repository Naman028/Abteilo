"""Deterministic data collection for generated reports.

The model does not choose what a report covers. For each supported report kind the backend enumerates the
records the requester may see (the same permission checks as the business screens and the assistant tools),
builds the factual tables directly from those records, and prepares bounded evidence items from which the
model may only write narrative (summary, observations, next steps), which is then verified.

Supported kinds, by workspace:
- engineering_progress   Engineering: every project of the department, its tasks, recorded blockers and
                         dependencies (reviewed relationships), and the most relevant current document.
- finance_status         Finance: budgets per department and period, purchase requests by state.
- hr_leave               Human Resources: leave requests waiting for a decision.
- it_access              IT & Security: access requests by state.
- company_status         Owner: pending approvals, blocked projects, budget pressure, departments.

Bounds: at most MAX_SECTIONS sections and MAX_ROWS rows per table. Anything beyond is not silently dropped:
the report is marked partial and the limitation says what was left out.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Connection, text

from abteilo.ai.evidence import CAUSAL_EDGE_KINDS
from abteilo.ai.tools import TOOLS, Evidence, ProjectArgs, RelatedArgs, SearchArgs, StatusArgs, ToolContext, ToolDegraded
from abteilo.ai.tools import NoArgs, PeriodArgs, _money, _person
from abteilo.authz import Principal
from abteilo.modules import engineering, finance, hr, it

MAX_SECTIONS = 20
MAX_ROWS = 40

KIND_BY_TEMPLATE = {"engineering": "engineering_progress", "finance": "finance_status",
                    "human_resources": "hr_leave", "it_security": "it_access"}
TITLES = {"engineering_progress": "Engineering progress report", "finance_status": "Finance status report",
          "hr_leave": "Leave requests report", "it_access": "Access requests report",
          "company_status": "Company status report"}
MODULE = {"engineering_progress": "projects", "finance_status": "finance", "hr_leave": "hr", "it_access": "it",
          "company_status": None}


class Table(BaseModel):
    columns: list[str]
    rows: list[list[str]]


@dataclass
class SectionData:
    title: str
    status: str | None = None
    table: Table | None = None
    dependencies: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)   # what the model may write about, in order


@dataclass
class Collected:
    kind: str
    title: str
    period: str
    data_as_of: datetime
    overview: Table | None
    overview_evidence: list[Evidence]
    sections: list[SectionData]
    records_summary: str           # factual summary computed from the records (used when the model's is unsupported)
    limitations: list[str]
    sources_used: list[str]        # tool names, for the reply and the audit trail

    @property
    def complete(self) -> bool:
        return not self.limitations


def kind_for(template: str | None) -> str | None:
    """Report kind of a workspace; None = owner workspace when template is None and the caller is the owner."""
    return "company_status" if template is None else KIND_BY_TEMPLATE.get(template)


def authorized(p: Principal, kind: str, department_id: UUID | None) -> bool:
    """The permission a report needs, checked at generation and again at every download."""
    if not p.can("ai.chat.use"):
        return False
    if kind == "company_status":
        return p.has_company_wide("report.executive.read")
    if department_id is None or not p.can("department.member", department_id):
        return False
    return {
        "engineering_progress": lambda: p.can("engineering.project.read", department_id),
        "finance_status": lambda: p.can("finance.budget.read", department_id),
        "hr_leave": lambda: p.can("hr.leave.approve", department_id),
        "it_access": lambda: it.can_view_access(p, department_id),
    }[kind]()


def _counts(values) -> str:
    return ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in Counter(values).most_common())


def _bounded(rows: list, what: str, limitations: list[str], limit: int = MAX_ROWS) -> list:
    if len(rows) > limit:
        limitations.append(f"Only the first {limit} of {len(rows)} {what} are listed.")
    return rows[:limit]


def _now(conn: Connection) -> datetime:
    return conn.execute(text("SELECT now()")).scalar_one()


def _run(ctx: ToolContext, allowed: set[str], name: str, args, sources: list[str], limitations: list[str]) -> list[Evidence]:
    if name not in allowed:
        return []
    try:
        evidence = TOOLS[name].run(ctx, args)
    except ToolDegraded as exc:
        limitations.append(f"{name} did not answer in time ({exc.code}); its information is missing.")
        return []
    if name not in sources:
        sources.append(name)
    return evidence


# --- collectors ---------------------------------------------------------------------------------------
def _engineering(ctx: ToolContext, allowed: set[str]) -> Collected:
    conn, p, d = ctx.conn, ctx.principal, ctx.department_id
    limitations: list[str] = []
    sources = ["engineering.list_projects"]
    projects = engineering.list_projects(conn, p, d)
    overview = Table(columns=["Project", "Name", "Status", "Open tasks", "Blocked tasks", "Last update"],
                     rows=[[x.code, x.name, x.status, str(x.open_tasks), str(x.blocked_tasks), f"{x.updated_at:%d.%m.%Y}"]
                           for x in projects])
    overview_evidence = _run(ctx, allowed, "engineering.list_projects", NoArgs(), sources, limitations)
    sections, all_tasks = [], []
    for proj in _bounded(projects, "projects", limitations, MAX_SECTIONS):
        _, tasks = engineering.get_project(conn, p, proj.id)
        all_tasks += tasks
        rows = [[t.title, t.status.replace("_", " "), _person(t.assignee_name, t.assignee_active),
                 f"{t.due_date:%d.%m.%Y}" if t.due_date else "—"] for t in _bounded(tasks, f"tasks of {proj.code}", limitations)]
        section = SectionData(title=f"{proj.code} · {proj.name}", status=proj.status,
                              table=Table(columns=["Task", "Status", "Responsible", "Due"], rows=rows) if rows else None)
        section.evidence += _run(ctx, allowed, "engineering.get_project", ProjectArgs(project_code=proj.code), sources, limitations)
        if proj.status == "blocked" or proj.blocked_tasks:
            for rel in _run(ctx, allowed, "graph.related", RelatedArgs(subject=proj.code, depth=1), sources, limitations):
                section.evidence.append(rel)
                section.dependencies += [f"{e.src_label} — {e.kind.replace('_', ' ')} → {e.dst_label}"
                                         f" ({'reviewed' if e.authority == 'reviewed' else e.authority.replace('_', ' ')})"
                                         for e in rel.edges if e.kind in CAUSAL_EDGE_KINDS]
        section.evidence += _run(ctx, allowed, "documents.search", SearchArgs(query=f"{proj.code} {proj.name}"),
                                 sources, limitations)[:1]
        sections.append(section)
    tasks_by_status = Counter(t.status for t in all_tasks)
    blocked = [f"{x.code} ({x.name})" for x in projects if x.status == "blocked"]
    summary = (f"{len(projects)} project{'s' if len(projects) != 1 else ''}: {_counts(x.status for x in projects) or 'none'}. "
               f"{len(all_tasks)} tasks: " + (", ".join(f"{tasks_by_status[s]} {s.replace('_', ' ')}" for s in
                                                        ("done", "in_progress", "blocked", "todo") if tasks_by_status[s]) or "none")
               + "." + (f" Blocked: {', '.join(blocked)}." if blocked else ""))
    return Collected("engineering_progress", TITLES["engineering_progress"], "Current state of all projects",
                     _now(conn), overview, overview_evidence, sections, summary, limitations, sources)


def _finance(ctx: ToolContext, allowed: set[str]) -> Collected:
    conn, p, d = ctx.conn, ctx.principal, ctx.department_id
    limitations: list[str] = []
    sources: list[str] = []
    budgets = _bounded(finance.budget_summary(conn, p, d, None), "budget lines", limitations)
    overview = Table(columns=["Department", "Period", "Budget", "Approved", "Pending", "Remaining"],
                     rows=[[b.department_name, b.period, _money(b.budget_cents), _money(b.approved_cents),
                            _money(b.pending_cents), _money(b.remaining_cents)] for b in budgets])
    overview_evidence = _run(ctx, allowed, "finance.budget_summary", PeriodArgs(), sources, limitations)
    requests = finance.list_purchase_requests(conn, p, d, None)
    sections = []
    for title, states in (("Purchase requests awaiting a decision", ("submitted",)),
                          ("Decided purchase requests", ("approved", "rejected", "cancelled"))):
        rows = [r for r in requests if r.status in states]
        if not rows:
            continue
        table = Table(columns=["Request", "Department", "Amount", "Period", "Status", "Requested by"],
                      rows=[[r.title, r.department_name, _money(r.amount_cents), r.period, r.status,
                             _person(r.requester_name, r.requester_active)] for r in _bounded(rows, title.lower(), limitations)])
        sections.append(SectionData(title=title, table=table))
    if sections:
        sections[0].evidence += _run(ctx, allowed, "finance.list_purchase_requests", StatusArgs(), sources, limitations)
    pending = [r for r in requests if r.status == "submitted"]
    summary = (f"{len(budgets)} budget line{'s' if len(budgets) != 1 else ''} recorded. "
               f"{len(pending)} purchase request{'s' if len(pending) != 1 else ''} awaiting a decision"
               f" ({_money(sum(r.amount_cents for r in pending))}); {len(requests) - len(pending)} decided.")
    return Collected("finance_status", TITLES["finance_status"], "All recorded budget periods", _now(conn), overview,
                     overview_evidence, sections, summary, limitations, sources)


def _hr(ctx: ToolContext, allowed: set[str]) -> Collected:
    conn, p, d = ctx.conn, ctx.principal, ctx.department_id
    limitations: list[str] = []
    sources: list[str] = []
    pending = hr.pending_leave(conn, p, d)
    rows = [[_person(r.employee_name, r.employee_active), f"{r.start_date:%d.%m.%Y}", f"{r.end_date:%d.%m.%Y}", str(r.days),
             r.status] for r in _bounded(pending, "pending leave requests", limitations)]
    section = SectionData(title="Leave requests awaiting a decision",
                          table=Table(columns=["Employee", "From", "To", "Days", "Status"], rows=rows) if rows else None)
    section.evidence += _run(ctx, allowed, "hr.pending_leave", NoArgs(), sources, limitations)
    days = sum((r.days for r in pending), 0)
    summary = f"{len(pending)} leave request{'s' if len(pending) != 1 else ''} awaiting a decision, {days} days in total."
    return Collected("hr_leave", TITLES["hr_leave"], "Requests currently awaiting a decision", _now(conn), None, [],
                     [section], summary, limitations, sources)


def _it(ctx: ToolContext, allowed: set[str]) -> Collected:
    conn, p, d = ctx.conn, ctx.principal, ctx.department_id
    limitations: list[str] = []
    sources: list[str] = []
    requests = it.list_access_requests(conn, p, d, None)
    sections = []
    for title, states in (("Access requests awaiting approval", ("proposed",)), ("Decided access requests", ("approved", "rejected"))):
        rows = [r for r in requests if r.status in states]
        if not rows:
            continue
        table = Table(columns=["Person", "Role", "Scope", "Elevated", "Status", "Proposed by"],
                      rows=[[_person(r.target_name, r.target_active), r.role_code, r.department_name or "company",
                             "yes" if r.elevated else "no", r.status, _person(r.proposer_name, r.proposer_active)]
                            for r in _bounded(rows, title.lower(), limitations)])
        sections.append(SectionData(title=title, table=table))
    if sections:
        sections[0].evidence += _run(ctx, allowed, "it.list_access_requests", StatusArgs(), sources, limitations)
    open_ = [r for r in requests if r.status == "proposed"]
    summary = (f"{len(open_)} access request{'s' if len(open_) != 1 else ''} awaiting approval"
               f" ({sum(r.elevated for r in open_)} elevated); {len(requests) - len(open_)} decided.")
    return Collected("it_access", TITLES["it_access"], "All recorded access requests", _now(conn), None, [], sections,
                     summary, limitations, sources)


def _company(ctx: ToolContext, allowed: set[str]) -> Collected:
    from abteilo.overview import build_overview

    conn, p = ctx.conn, ctx.principal
    limitations: list[str] = []
    sources: list[str] = []
    o = build_overview(conn, p)
    pend = o.pending
    overview = Table(columns=["Waiting for a decision", "Count"],
                     rows=[["Purchase requests", f"{pend.purchase_requests} ({_money(pend.purchase_amount_cents)})"],
                           ["Leave requests", str(pend.leave_requests)], ["Access requests", str(pend.access_requests)],
                           ["of which elevated", str(pend.elevated_access_requests)]])
    overview_evidence = _run(ctx, allowed, "company.overview", NoArgs(), sources, limitations)
    sections = [SectionData(title="Departments", table=Table(
        columns=["Department", "Members", "Active projects", "Blocked projects", "Pending purchases", "Documents"],
        rows=[[x.name, str(x.members), str(x.active_projects), str(x.blocked_projects), str(x.pending_purchases),
               str(x.documents)] for x in _bounded(o.departments, "departments", limitations)]))]
    if o.blocked_projects:
        sections.append(SectionData(title="Blocked projects", table=Table(
            columns=["Project", "Name", "Department", "Blocked tasks"],
            rows=[[b.code, b.name, b.department_name, ", ".join(b.blocked_tasks) or "none recorded"]
                  for b in _bounded(o.blocked_projects, "blocked projects", limitations)])))
    if o.budget_pressure:
        sections.append(SectionData(title="Budget pressure", table=Table(
            columns=["Department", "Period", "Budget", "Approved", "Pending"],
            rows=[[b.department_name, b.period, _money(b.budget_cents), _money(b.approved_cents), _money(b.pending_cents)]
                  for b in _bounded(o.budget_pressure, "budget lines", limitations)])))
    summary = (f"{o.active_members} active members in {o.active_departments} departments. Waiting for a decision: "
               f"{pend.purchase_requests} purchase, {pend.leave_requests} leave and {pend.access_requests} access requests. "
               f"{len(o.blocked_projects)} blocked project{'s' if len(o.blocked_projects) != 1 else ''}.")
    return Collected("company_status", TITLES["company_status"], f"Current state, budget period {o.period}", _now(conn),
                     overview, overview_evidence, sections, summary, limitations, sources)


COLLECTORS = {"engineering_progress": _engineering, "finance_status": _finance, "hr_leave": _hr, "it_access": _it,
              "company_status": _company}


def collect(conn: Connection, p: Principal, kind: str, department_id: UUID | None, allowed: set[str]) -> Collected:
    """`allowed` = the assistant tools this principal may use in this workspace (permission and module checks)."""
    return COLLECTORS[kind](ToolContext(conn, p, department_id, ""), allowed)
