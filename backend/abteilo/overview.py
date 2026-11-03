"""Owner overview: what needs attention across the company, computed directly
from the database (no model involved), and the audit export.

This is an approved executive reporting view: business status, amounts and
counts. It shows no chat content, no salaries or leave details of individuals,
and AI usage only per department.
"""

import csv
import io
import json
from datetime import datetime
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import Connection, text

from abteilo import audit
from abteilo.authz import Principal
from abteilo.config import get_settings
from abteilo.deps import current_principal, tx
from abteilo.modules.it import AccessRequestView, _AR_SQL

router = APIRouter(prefix="/api/admin", tags=["admin"])

# Cells starting with these can be executed as formulas by spreadsheet software (OWASP CSV injection).
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "\n")
_CURRENT_PERIOD = "to_char(now(), 'YYYY') || '-Q' || to_char(now(), 'Q')"


class Pending(BaseModel):
    purchase_requests: int
    purchase_amount_cents: int
    leave_requests: int
    access_requests: int
    elevated_access_requests: int


class BlockedProject(BaseModel):
    code: str
    name: str
    department_name: str
    blocked_tasks: list[str]


class BudgetPressure(BaseModel):
    department_name: str
    period: str
    budget_cents: int
    approved_cents: int
    pending_cents: int


class DepartmentStatus(BaseModel):
    department_id: UUID
    name: str
    members: int
    active_projects: int
    blocked_projects: int
    pending_purchases: int
    documents: int


class AiHealth(BaseModel):
    calls_7d: int
    errors_7d: int
    input_tokens_7d: int
    output_tokens_7d: int
    enabled_models: int
    external_ai_enabled: bool
    ollama_reachable: bool


class ActivityCount(BaseModel):
    action: str
    count: int


class Overview(BaseModel):
    company_name: str
    generated_at: datetime
    active_members: int
    active_departments: int
    period: str
    pending: Pending
    blocked_projects: list[BlockedProject]
    budget_pressure: list[BudgetPressure]
    departments: list[DepartmentStatus]
    ai: AiHealth
    activity_24h: list[ActivityCount]
    elevated_requests: list[AccessRequestView]


def build_overview(conn: Connection, p: Principal) -> Overview:
    head = conn.execute(text(
        f"SELECT c.name, now() AS generated_at, {_CURRENT_PERIOD} AS period,"
        " (SELECT count(*) FROM core.memberships m WHERE m.company_id = c.id AND m.status = 'active') AS members,"
        " (SELECT count(*) FROM core.departments d WHERE d.status = 'active') AS departments,"
        " (SELECT external_ai_enabled FROM core.company_settings) AS external_ai"
        " FROM core.companies c WHERE c.id = core.current_company_id()"
    )).one()
    pending = conn.execute(text(
        "SELECT (SELECT count(*) FROM core.purchase_requests WHERE status = 'submitted') AS purchase_requests,"
        " (SELECT coalesce(sum(amount_cents), 0) FROM core.purchase_requests WHERE status = 'submitted') AS purchase_amount_cents,"
        " (SELECT count(*) FROM core.leave_requests WHERE status = 'submitted') AS leave_requests,"
        " (SELECT count(*) FROM core.access_requests ar JOIN core.roles r ON r.company_id = ar.company_id"
        "   AND r.code = ar.role_code WHERE ar.status = 'proposed' AND NOT r.elevated) AS access_requests,"
        " (SELECT count(*) FROM core.access_requests ar JOIN core.roles r ON r.company_id = ar.company_id"
        "   AND r.code = ar.role_code WHERE ar.status = 'proposed' AND r.elevated) AS elevated_access_requests"
    )).mappings().one()
    blocked = conn.execute(text(
        "SELECT p.code, p.name, d.name AS department_name,"
        " coalesce(array_agg(t.title ORDER BY t.title) FILTER (WHERE t.status = 'blocked'), '{}') AS blocked_tasks"
        " FROM core.projects p JOIN core.departments d ON d.company_id = p.company_id AND d.id = p.department_id"
        " LEFT JOIN core.tasks t ON t.company_id = p.company_id AND t.project_id = p.id"
        " WHERE p.status = 'blocked' OR t.status = 'blocked'"
        " GROUP BY p.code, p.name, d.name ORDER BY p.code LIMIT 20"
    )).mappings()
    pressure = conn.execute(text(
        "SELECT d.name AS department_name, b.period, b.amount_cents AS budget_cents,"
        " coalesce(sum(pr.amount_cents) FILTER (WHERE pr.status = 'approved'), 0) AS approved_cents,"
        " coalesce(sum(pr.amount_cents) FILTER (WHERE pr.status = 'submitted'), 0) AS pending_cents"
        " FROM core.budgets b JOIN core.departments d ON d.company_id = b.company_id AND d.id = b.department_id"
        " LEFT JOIN core.purchase_requests pr ON pr.company_id = b.company_id AND pr.department_id = b.department_id"
        "   AND pr.period = b.period"
        f" WHERE b.period = {_CURRENT_PERIOD}"
        " GROUP BY d.name, b.period, b.amount_cents"
        # Flag departments where approved + pending spend reaches 80% of the budget.
        " HAVING coalesce(sum(pr.amount_cents) FILTER (WHERE pr.status IN ('approved', 'submitted')), 0) >= 0.8 * b.amount_cents"
        " ORDER BY d.name"
    )).mappings()
    departments = conn.execute(text(
        "SELECT d.id AS department_id, d.name,"
        " (SELECT count(DISTINCT ra.user_id) FROM core.role_assignments ra WHERE ra.department_id = d.id AND ra.revoked_at IS NULL) AS members,"
        " (SELECT count(*) FROM core.projects x WHERE x.department_id = d.id AND x.status = 'active') AS active_projects,"
        " (SELECT count(*) FROM core.projects x WHERE x.department_id = d.id AND x.status = 'blocked') AS blocked_projects,"
        " (SELECT count(*) FROM core.purchase_requests x WHERE x.department_id = d.id AND x.status = 'submitted') AS pending_purchases,"
        " (SELECT count(*) FROM core.documents x WHERE x.department_id = d.id AND x.status = 'active') AS documents"
        " FROM core.departments d WHERE d.status = 'active' ORDER BY d.name"
    )).mappings()
    ai = conn.execute(text(
        "SELECT count(*) AS calls, count(*) FILTER (WHERE status <> 'ok') AS errors,"
        " coalesce(sum(input_tokens), 0) AS input_tokens, coalesce(sum(output_tokens), 0) AS output_tokens,"
        " (SELECT count(*) FROM core.model_profiles WHERE enabled) AS enabled_models"
        " FROM core.ai_runs WHERE created_at > now() - interval '7 days'"
    )).one()
    activity = conn.execute(text(
        "SELECT action, count(*) AS count FROM core.audit_events WHERE occurred_at > now() - interval '24 hours'"
        " AND action NOT LIKE 'auth.%' GROUP BY action ORDER BY count DESC, action LIMIT 20"
    )).mappings()
    elevated: list[AccessRequestView] = []
    if p.has_company_wide("role.elevated.approve"):
        elevated = [AccessRequestView(**r) for r in conn.execute(
            text(_AR_SQL.format(where="ar.status = 'proposed' AND r.elevated"))).mappings()]

    return Overview(
        company_name=head.name, generated_at=head.generated_at, active_members=head.members,
        active_departments=head.departments, period=head.period, pending=Pending(**pending),
        blocked_projects=[BlockedProject(**b) for b in blocked], budget_pressure=[BudgetPressure(**b) for b in pressure],
        departments=[DepartmentStatus(**d) for d in departments],
        ai=AiHealth(calls_7d=ai.calls, errors_7d=ai.errors, input_tokens_7d=ai.input_tokens,
                    output_tokens_7d=ai.output_tokens, enabled_models=ai.enabled_models,
                    external_ai_enabled=bool(head.external_ai), ollama_reachable=_ollama_up()),
        activity_24h=[ActivityCount(**a) for a in activity],
        elevated_requests=elevated,
    )


def _ollama_up() -> bool:
    try:
        return httpx.get(f"{get_settings().ollama_url.rstrip('/')}/api/version", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


@router.get("/overview", response_model=Overview)
def api_overview(p: Principal = Depends(current_principal)):
    p.require("report.executive.read")
    with tx(p) as conn:
        return build_overview(conn, p)


@router.get("/audit.csv")
def api_audit_csv(p: Principal = Depends(current_principal)):
    """Last 10,000 audit events as CSV for compliance reviews."""
    p.require("audit.read")
    with tx(p) as conn:
        rows = conn.execute(text(
            "SELECT a.id, a.occurred_at, u.display_name AS actor, a.action, a.resource_type, a.resource_id, a.details"
            " FROM core.audit_events a LEFT JOIN core.users u ON u.id = a.actor_user_id ORDER BY a.id DESC LIMIT 10000"
        )).all()
        audit.record(conn, p.company_id, p.user_id, "audit.exported", "company", p.company_id, {"rows": len(rows)})
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "occurred_at", "actor", "action", "resource_type", "resource_id", "details"])
    for r in rows:
        # Neutralise spreadsheet formula injection in free-text cells.
        cells = [r.id, r.occurred_at.isoformat(), r.actor or "system", r.action, r.resource_type, r.resource_id or "",
                 json.dumps(r.details, ensure_ascii=False)]
        writer.writerow([f"'{c}" if isinstance(c, str) and c[:1] in _FORMULA_START else c for c in cells])
    return Response(buffer.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="abteilo-audit.csv"'})
