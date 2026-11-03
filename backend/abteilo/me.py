"""Authentication, the signed-in user's workspace, and GDPR data-subject rights.

Art. 15/20 GDPR: GET /api/me/export returns the user's personal data as JSON.
Art. 17 GDPR: users delete their own chats; account data tied to business
records (leave, purchases, audit) is retained under legal retention duties
(HGB §257, AO §147) and handled by the controller, not self-service.
"""

import json
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, EmailStr, StringConstraints
from sqlalchemy import text

from abteilo import audit, security
from abteilo.authz import Principal
from abteilo.config import get_settings
from abteilo.deps import SESSION_COOKIE, current_principal, tx

router = APIRouter(prefix="/api", tags=["me"])


class LoginBody(BaseModel):
    email: EmailStr
    password: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    company_slug: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")] | None = None


class WorkspaceView(BaseModel):
    department_id: UUID
    code: str
    name: str
    template: str
    modules: list[str]
    permissions: list[str]


class MeView(BaseModel):
    user_id: UUID
    email: str
    display_name: str
    company_id: UUID
    company_name: str
    company_permissions: list[str]
    workspaces: list[WorkspaceView]
    departments: list[dict]


def _me(p: Principal) -> MeView:
    with tx(p) as conn:
        company_name = conn.execute(text("SELECT name FROM core.companies WHERE id = :c"), {"c": p.company_id}).scalar_one()
        depts = conn.execute(text(
            "SELECT d.id, d.code, d.name, d.template, coalesce(array_agg(m.module_code ORDER BY m.module_code)"
            " FILTER (WHERE m.module_code IS NOT NULL), '{}') AS modules FROM core.departments d"
            " LEFT JOIN core.department_modules m ON m.company_id = d.company_id AND m.department_id = d.id"
            " WHERE d.status = 'active' GROUP BY d.id ORDER BY d.name"
        )).mappings().all()
    workspaces = [
        WorkspaceView(department_id=d["id"], code=d["code"], name=d["name"], template=d["template"], modules=d["modules"],
                      permissions=sorted(g.permission for g in p.grants if g.department_id == d["id"]))
        for d in depts if d["id"] in p.member_departments
    ]
    return MeView(
        user_id=p.user_id, email=p.email, display_name=p.display_name, company_id=p.company_id, company_name=company_name,
        company_permissions=sorted({g.permission for g in p.grants if g.department_id is None}),
        workspaces=workspaces,
        departments=[{"id": str(d["id"]), "name": d["name"]} for d in depts],
    )


@router.post("/auth/login", response_model=MeView)
def api_login(body: LoginBody, response: Response):
    session = security.login(body.email, body.password, body.company_slug)
    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE, session.token, httponly=True, secure=settings.cookie_secure, samesite="strict",
        max_age=settings.session_ttl_hours * 3600, path="/",
    )
    return _me(security.resolve_session(session.token))


@router.post("/auth/logout", status_code=204)
def api_logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        security.revoke_session(token)
    response.delete_cookie(SESSION_COOKIE, path="/")


class ChangePassword(BaseModel):
    current_password: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    new_password: Annotated[str, StringConstraints(min_length=security.MIN_PASSWORD_LENGTH, max_length=256)]


@router.post("/me/password", status_code=204)
def api_change_password(body: ChangePassword, request: Request, p: Principal = Depends(current_principal)):
    """Change your own password; your other sessions are signed out, this one stays signed in."""
    security.change_own_password(p, request.cookies.get(SESSION_COOKIE, ""), body.current_password, body.new_password)


@router.get("/me", response_model=MeView)
def api_me(p: Principal = Depends(current_principal)):
    return _me(p)


@router.get("/me/export")
def api_export(p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        def rows(sql: str) -> list[dict]:
            return [dict(r) for r in conn.execute(text(sql), {"u": p.user_id}).mappings()]

        data = {
            "exported_at": datetime.now(UTC).isoformat(),
            "profile": rows("SELECT id, email, display_name, created_at, last_login_at FROM core.users WHERE id = :u"),
            "membership": rows("SELECT title, status, joined_at FROM core.memberships"
                               " WHERE user_id = :u AND company_id = core.current_company_id()"),
            "roles": rows("SELECT ra.role_code, d.name AS department, ra.granted_at, ra.revoked_at FROM core.role_assignments ra"
                          " LEFT JOIN core.departments d ON d.id = ra.department_id WHERE ra.user_id = :u"),
            "leave_balances": rows("SELECT year, days_total FROM core.leave_balances WHERE user_id = :u"),
            "leave_requests": rows("SELECT start_date, end_date, days, status, created_at FROM core.leave_requests WHERE user_id = :u"),
            "purchase_requests": rows("SELECT title, amount_cents, period, status, created_at FROM core.purchase_requests"
                                      " WHERE requested_by = :u"),
            "conversations": rows("SELECT c.title, m.role, m.content, m.created_at FROM conv.conversations c"
                                  " JOIN conv.messages m ON m.conversation_id = c.id WHERE c.user_id = :u ORDER BY m.created_at"),
            "ai_usage": rows("SELECT created_at, profile_code, stage, status, input_tokens, output_tokens FROM core.ai_runs"
                             " WHERE user_id = :u ORDER BY created_at"),
            "audit_events_as_actor": rows("SELECT occurred_at, action, resource_type FROM core.audit_events"
                                          " WHERE actor_user_id = :u ORDER BY occurred_at"),
        }
        audit.record(conn, p.company_id, p.user_id, "privacy.data_exported", "user", p.user_id)
    return Response(
        content=json.dumps(data, default=str, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="abteilo-my-data.json"'},
    )
