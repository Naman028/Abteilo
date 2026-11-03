"""Owner / admin console: company settings, departments, members, models,
usage tracking, data inventory, retention and audit.

The owner manages the company but this console never exposes chat content or
individual employees' business records. Usage is reported per department and
model, not per person (BetrVG / BDSG: no covert performance monitoring).
"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, EmailStr, Field, StringConstraints
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from abteilo import audit, company, retention
from abteilo.ai import gateway
from abteilo.ai.embeddings import get_embedder
from abteilo.ai.providers import EXTERNAL_PROVIDERS, ChatMessage
from abteilo.authz import Principal
from abteilo.config import get_settings
from abteilo.db import set_clause
from abteilo.deps import current_principal, load_department, tx
from abteilo.indexer import retry_failed
from abteilo.errors import Conflict, Forbidden, Invalid, NotFound
from abteilo.modules.it import revoke_assignment
from abteilo.security import hash_password, revoke_user_sessions
from abteilo.templates import DEPARTMENT_TEMPLATES, ROLE_TEMPLATES

router = APIRouter(prefix="/api/admin", tags=["admin"])

Code = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,40}$")]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=80)]


# --- settings -----------------------------------------------------------------------
class SettingsView(BaseModel):
    company_name: str
    timezone: str
    chat_retention_days: Literal[30, 60] | None
    external_ai_enabled: bool
    external_ai_confirmed_at: datetime | None


class UpdateSettings(BaseModel):
    chat_retention_days: Literal[30, 60] | None
    external_ai_enabled: bool
    # Required to enable external AI: the owner confirms a data processing agreement
    # (Art. 28 GDPR) and transfer assessment exist for the configured provider.
    confirm_processing_agreement: bool = False


def _settings(conn) -> SettingsView:
    row = conn.execute(text(
        "SELECT c.name AS company_name, c.timezone, s.chat_retention_days, s.external_ai_enabled, s.external_ai_confirmed_at"
        " FROM core.company_settings s JOIN core.companies c ON c.id = s.company_id"
    )).mappings().one()
    return SettingsView(**row)


@router.get("/settings", response_model=SettingsView)
def api_get_settings(p: Principal = Depends(current_principal)):
    p.require("company.settings.manage")
    with tx(p) as conn:
        return _settings(conn)


@router.put("/settings", response_model=SettingsView)
def api_update_settings(body: UpdateSettings, p: Principal = Depends(current_principal)):
    p.require("company.settings.manage")
    if body.external_ai_enabled and not body.confirm_processing_agreement:
        raise Invalid("Confirm the data processing agreement before enabling external AI providers.")
    with tx(p) as conn:
        before = _settings(conn)
        conn.execute(
            text("UPDATE core.company_settings SET chat_retention_days = :r, external_ai_enabled = :e,"
                 " external_ai_confirmed_by = CASE WHEN :e THEN coalesce(CASE WHEN external_ai_enabled THEN external_ai_confirmed_by END, :u) END,"
                 " external_ai_confirmed_at = CASE WHEN :e THEN coalesce(CASE WHEN external_ai_enabled THEN external_ai_confirmed_at END, now()) END,"
                 " updated_at = now()"),
            {"r": body.chat_retention_days, "e": body.external_ai_enabled, "u": p.user_id},
        )
        audit.record(conn, p.company_id, p.user_id, "company.settings_changed", "company", p.company_id, {
            "chat_retention_days": [before.chat_retention_days, body.chat_retention_days],
            "external_ai_enabled": [before.external_ai_enabled, body.external_ai_enabled],
        })
        return _settings(conn)


class CompanyProfile(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=160)]
    timezone: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_]+(/[A-Za-z_+-]+){0,2}$")]


@router.put("/company", response_model=CompanyProfile)
def api_update_company(body: CompanyProfile, p: Principal = Depends(current_principal)):
    p.require("company.settings.manage")
    with tx(p) as conn:
        conn.execute(text("UPDATE core.companies SET name = :n, timezone = :tz WHERE id = core.current_company_id()"),
                     {"n": body.name, "tz": body.timezone})
        audit.record(conn, p.company_id, p.user_id, "company.profile_changed", "company", p.company_id)
        return body


# --- departments --------------------------------------------------------------------
class DepartmentAdminView(BaseModel):
    id: UUID
    code: str
    name: str
    template: str
    status: Literal["active", "archived"]
    modules: list[str]
    members: int
    created_at: datetime


class CreateDepartment(BaseModel):
    code: Code
    name: Name
    template: str = "custom"


class UpdateDepartment(BaseModel):
    name: Name | None = None
    status: Literal["active", "archived"] | None = None


class TemplateView(BaseModel):
    code: str
    name: str
    modules: list[str]
    tools: list[str]


_DEPT_SQL = """
SELECT d.id, d.code, d.name, d.template, d.status, d.created_at,
  coalesce((SELECT array_agg(m.module_code ORDER BY m.module_code) FROM core.department_modules m
            WHERE m.company_id = d.company_id AND m.department_id = d.id), '{{}}') AS modules,
  (SELECT count(DISTINCT ra.user_id) FROM core.role_assignments ra
    WHERE ra.department_id = d.id AND ra.revoked_at IS NULL) AS members
FROM core.departments d WHERE {where} ORDER BY d.status, d.name
"""


@router.get("/department-templates", response_model=list[TemplateView])
def api_templates(p: Principal = Depends(current_principal)):
    p.require("department.manage")
    return [TemplateView(code=t.code, name=t.name, modules=list(t.modules), tools=list(t.tools))
            for t in DEPARTMENT_TEMPLATES.values()]


@router.get("/departments", response_model=list[DepartmentAdminView])
def api_departments(p: Principal = Depends(current_principal)):
    p.require("department.manage")
    with tx(p) as conn:
        return [DepartmentAdminView(**r) for r in conn.execute(text(_DEPT_SQL.format(where="true"))).mappings()]


@router.post("/departments", response_model=DepartmentAdminView, status_code=201)
def api_create_department(body: CreateDepartment, p: Principal = Depends(current_principal)):
    p.require("department.manage")
    with tx(p) as conn:
        dept_id = company.add_department(conn, p.company_id, p.user_id, body.code, body.name, body.template)
        return DepartmentAdminView(**conn.execute(text(_DEPT_SQL.format(where="d.id = :id")), {"id": dept_id}).mappings().one())


@router.patch("/departments/{department_id}", response_model=DepartmentAdminView)
def api_update_department(department_id: UUID, body: UpdateDepartment, p: Principal = Depends(current_principal)):
    p.require("department.manage")
    with tx(p) as conn:
        changes = body.model_dump(exclude_unset=True, exclude_none=True)
        if changes:
            if conn.execute(text(f"UPDATE core.departments SET {set_clause(changes)} WHERE id = :id RETURNING id"),
                            {**changes, "id": department_id}).first() is None:
                raise NotFound()
            # Archiving hides a department; its records stay untouched (DATA-01).
            audit.record(conn, p.company_id, p.user_id, "department.updated", "department", department_id, changes)
        row = conn.execute(text(_DEPT_SQL.format(where="d.id = :id")), {"id": department_id}).mappings().first()
        if row is None:
            raise NotFound()
        return DepartmentAdminView(**row)


class SetModules(BaseModel):
    modules: list[Annotated[str, StringConstraints(pattern=r"^[a-z_]{2,30}$")]] = Field(max_length=10)


class DepartmentMember(BaseModel):
    user_id: UUID
    display_name: str
    title: str | None
    roles: list[str]


@router.put("/departments/{department_id}/modules", response_model=DepartmentAdminView)
def api_set_modules(department_id: UUID, body: SetModules, p: Principal = Depends(current_principal)):
    """Switch a department's modules on or off, within what its template supports."""
    p.require("department.manage")
    with tx(p) as conn:
        template = conn.execute(text("SELECT template FROM core.departments WHERE id = :d"), {"d": department_id}).scalar()
        if template is None:
            raise NotFound()
        wanted, supported = set(body.modules), set(DEPARTMENT_TEMPLATES[template].modules)
        if not wanted <= supported:
            raise Invalid(f"This department supports: {', '.join(sorted(supported))}.")
        conn.execute(text("DELETE FROM core.department_modules WHERE department_id = :d AND NOT (module_code = ANY(:m))"),
                     {"d": department_id, "m": list(wanted)})
        for module in wanted:
            conn.execute(text("INSERT INTO core.department_modules (company_id, department_id, module_code)"
                              " VALUES (core.current_company_id(), :d, :m) ON CONFLICT DO NOTHING"),
                         {"d": department_id, "m": module})
        audit.record(conn, p.company_id, p.user_id, "department.modules_set", "department", department_id,
                     {"modules": sorted(wanted)})
        return DepartmentAdminView(**conn.execute(text(_DEPT_SQL.format(where="d.id = :id")), {"id": department_id}).mappings().one())


@router.get("/departments/{department_id}/members", response_model=list[DepartmentMember])
def api_department_members(department_id: UUID, p: Principal = Depends(current_principal)):
    p.require("department.manage")
    with tx(p) as conn:
        rows = conn.execute(text(
            "SELECT ra.user_id, u.display_name, m.title, array_agg(ra.role_code ORDER BY ra.role_code) AS roles"
            " FROM core.role_assignments ra JOIN core.users u ON u.id = ra.user_id"
            " JOIN core.memberships m ON m.company_id = ra.company_id AND m.user_id = ra.user_id"
            " WHERE ra.department_id = :d AND ra.revoked_at IS NULL GROUP BY ra.user_id, u.display_name, m.title"
            " ORDER BY u.display_name"
        ), {"d": department_id}).mappings()
        return [DepartmentMember(**r) for r in rows]


# --- members ------------------------------------------------------------------------
class MemberRole(BaseModel):
    assignment_id: UUID
    role_code: str
    department_id: UUID | None
    department_name: str | None


class MemberAdminView(BaseModel):
    user_id: UUID
    email: str
    display_name: str
    title: str | None
    status: Literal["active", "disabled"]
    last_login_at: datetime | None
    roles: list[MemberRole]


class CreateMember(BaseModel):
    email: EmailStr
    display_name: Name
    title: Annotated[str, StringConstraints(max_length=80)] | None = None
    initial_password: Annotated[str, StringConstraints(min_length=12, max_length=256)]
    department_id: UUID | None = None
    department_role: Code | None = None


class UpdateMember(BaseModel):
    display_name: Name | None = None
    title: Annotated[str, StringConstraints(max_length=80)] | None = None
    status: Literal["active", "disabled"] | None = None


class ResetPassword(BaseModel):
    new_password: Annotated[str, StringConstraints(min_length=12, max_length=256)]


class AssignDepartmentRole(BaseModel):
    department_id: UUID
    role_code: Code


def _members(conn, user_id: UUID | None = None) -> list[MemberAdminView]:
    rows = conn.execute(text(
        "SELECT m.user_id, u.email, u.display_name, m.title, m.status, u.last_login_at FROM core.memberships m"
        " JOIN core.users u ON u.id = m.user_id WHERE m.company_id = core.current_company_id()"
        " AND (CAST(:u AS uuid) IS NULL OR m.user_id = :u) ORDER BY u.display_name LIMIT 1000"
    ), {"u": user_id}).mappings().all()
    roles = conn.execute(text(
        "SELECT ra.user_id, ra.id AS assignment_id, ra.role_code, ra.department_id, d.name AS department_name"
        " FROM core.role_assignments ra LEFT JOIN core.departments d ON d.company_id = ra.company_id AND d.id = ra.department_id"
        " WHERE ra.revoked_at IS NULL ORDER BY ra.role_code"
    )).mappings().all()
    by_user: dict[UUID, list[MemberRole]] = {}
    for r in roles:
        by_user.setdefault(r["user_id"], []).append(MemberRole(**{k: v for k, v in r.items() if k != "user_id"}))
    return [MemberAdminView(**r, roles=by_user.get(r["user_id"], [])) for r in rows]


_DEPARTMENT_ROLES = {r.code for r in ROLE_TEMPLATES if r.scope == "department"}


@router.get("/members", response_model=list[MemberAdminView])
def api_members(p: Principal = Depends(current_principal)):
    p.require("member.manage")
    with tx(p) as conn:
        return _members(conn)


@router.post("/members", response_model=MemberAdminView, status_code=201)
def api_create_member(body: CreateMember, p: Principal = Depends(current_principal)):
    """Adds an account with a department role. Company-wide roles are granted only
    through the IT access-request workflow (proposal + independent approval)."""
    p.require("member.manage")
    if (body.department_id is None) != (body.department_role is None):
        raise Invalid("Provide both a department and a department role, or neither.")
    if body.department_role is not None and body.department_role not in _DEPARTMENT_ROLES:
        raise Invalid("Unknown department role.")
    try:
        with tx(p) as conn:
            if body.department_id is not None:
                load_department(conn, body.department_id)
            user_id = company.add_member(conn, p.company_id, p.user_id, email=body.email, display_name=body.display_name,
                                         password=body.initial_password, title=body.title)
            if body.department_id is not None:
                company.assign_role(conn, p.company_id, user_id, body.department_role, body.department_id, p.user_id)
                audit.record(conn, p.company_id, p.user_id, "member.role_assigned", "user", user_id,
                             {"role": body.department_role, "department_id": str(body.department_id)})
            return _members(conn, user_id)[0]
    except IntegrityError as exc:
        raise Conflict("An account with this email already exists.") from exc


def _require_member(conn, user_id: UUID) -> None:
    if conn.execute(text("SELECT 1 FROM core.memberships WHERE company_id = core.current_company_id() AND user_id = :u"),
                    {"u": user_id}).first() is None:
        raise NotFound()


@router.patch("/members/{user_id}", response_model=MemberAdminView)
def api_update_member(user_id: UUID, body: UpdateMember, p: Principal = Depends(current_principal)):
    p.require("member.manage")
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if "status" in changes and user_id == p.user_id:
        raise Forbidden("You cannot change your own account status.")
    with tx(p) as conn:
        _require_member(conn, user_id)
        if "display_name" in changes:
            conn.execute(text("UPDATE core.users SET display_name = :n WHERE id = :u"), {"n": changes["display_name"], "u": user_id})
        if "title" in changes:
            conn.execute(text("UPDATE core.memberships SET title = :t WHERE company_id = core.current_company_id() AND user_id = :u"),
                         {"t": changes["title"], "u": user_id})
        if "status" in changes:
            set_member_status(conn, user_id, changes["status"])
        if changes:
            audit.record(conn, p.company_id, p.user_id, "member.updated", "user", user_id, {"fields": sorted(changes)})
        return _members(conn, user_id)[0]


def set_member_status(conn, user_id: UUID, status: str) -> None:
    """Owner-safe status change: lock the company row, change, re-check OWN-01."""
    company.lock_company(conn)
    conn.execute(text("UPDATE core.memberships SET status = :s WHERE company_id = core.current_company_id() AND user_id = :u"),
                 {"s": status, "u": user_id})
    # Keep the stored graph in step with the membership in the same transaction (the read-time guard is authoritative).
    conn.execute(text(
        "UPDATE core.graph_entities e SET active = (:s = 'active' AND u.status = 'active'), status = :s, projected_at = now()"
        " FROM core.users u WHERE u.id = e.ref_id AND e.kind = 'person' AND e.ref_id = :u AND e.quarantine_reason IS NULL"
        " AND (e.active, e.status) IS DISTINCT FROM (:s = 'active' AND u.status = 'active', :s)"), {"s": status, "u": user_id})
    if status == "disabled":
        company.ensure_active_owner(conn)
        revoke_user_sessions(conn, user_id)


@router.post("/members/{user_id}/password", status_code=204)
def api_reset_password(user_id: UUID, body: ResetPassword, p: Principal = Depends(current_principal)):
    """Sets a new initial password and signs the person out everywhere."""
    p.require("member.manage")
    if user_id == p.user_id:
        raise Forbidden("Change your own password from your account, not the console.")
    with tx(p) as conn:
        _require_member(conn, user_id)
        conn.execute(text("UPDATE core.users SET password_hash = :h WHERE id = :u"),
                     {"h": hash_password(body.new_password), "u": user_id})
        revoke_user_sessions(conn, user_id)
        audit.record(conn, p.company_id, p.user_id, "member.password_reset", "user", user_id)


@router.post("/members/{user_id}/roles", response_model=MemberAdminView, status_code=201)
def api_assign_department_role(user_id: UUID, body: AssignDepartmentRole, p: Principal = Depends(current_principal)):
    """Direct assignment is limited to department-scoped roles. Company-wide and
    elevated roles go through the access-request workflow."""
    p.require("member.manage")
    if body.role_code not in _DEPARTMENT_ROLES:
        raise Invalid("Only department roles can be assigned here; use an access request for company roles.")
    with tx(p) as conn:
        _require_member(conn, user_id)
        load_department(conn, body.department_id)
        company.assign_role(conn, p.company_id, user_id, body.role_code, body.department_id, p.user_id)
        audit.record(conn, p.company_id, p.user_id, "member.role_assigned", "user", user_id,
                     {"role": body.role_code, "department_id": str(body.department_id)})
        return _members(conn, user_id)[0]


@router.delete("/members/{user_id}/roles/{assignment_id}", response_model=MemberAdminView)
def api_revoke_role(user_id: UUID, assignment_id: UUID, p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        if conn.execute(text("SELECT 1 FROM core.role_assignments WHERE id = :a AND user_id = :u"),
                        {"a": assignment_id, "u": user_id}).first() is None:
            raise NotFound()
        revoke_assignment(conn, p, assignment_id)
        return _members(conn, user_id)[0]


# --- models and routing ----------------------------------------------------------------
Provider = Literal["ollama", "openai_compatible", "anthropic"]
ModelName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:/-]{1,100}$")]


class ModelProfileView(BaseModel):
    code: str
    display_name: str
    provider: str
    model_name: str
    purpose: str
    max_output_tokens: int
    temperature: Decimal
    enabled: bool
    external: bool


class CreateModelProfile(BaseModel):
    code: Code
    display_name: Name
    provider: Provider
    model_name: ModelName
    purpose: Annotated[str, StringConstraints(max_length=300)] = ""
    max_output_tokens: int = Field(1500, ge=64, le=16000)
    temperature: Decimal = Field(Decimal("0.2"), ge=0, le=1)
    enabled: bool = True


class UpdateModelProfile(BaseModel):
    display_name: Name | None = None
    model_name: ModelName | None = None
    purpose: Annotated[str, StringConstraints(max_length=300)] | None = None
    max_output_tokens: int | None = Field(None, ge=64, le=16000)
    temperature: Decimal | None = Field(None, ge=0, le=1)
    enabled: bool | None = None


class RouteView(BaseModel):
    department_id: UUID | None
    department_name: str | None
    task_class: Literal["general", "heavy"]
    profile_code: str


class SetRoute(BaseModel):
    department_id: UUID | None = None
    task_class: Literal["general", "heavy"]
    profile_code: Code


class ProviderStatus(BaseModel):
    provider: str
    configured: bool
    reachable: bool | None
    installed_models: list[str]


class ModelTestResult(BaseModel):
    ok: bool
    error_code: str | None


def _profiles(conn) -> list[ModelProfileView]:
    rows = conn.execute(text("SELECT code, display_name, provider, model_name, purpose, max_output_tokens, temperature,"
                             " enabled FROM core.model_profiles ORDER BY code")).mappings()
    return [ModelProfileView(**r, external=r["provider"] in EXTERNAL_PROVIDERS) for r in rows]


def _routes(conn) -> list[RouteView]:
    rows = conn.execute(text(
        "SELECT r.department_id, d.name AS department_name, r.task_class, r.profile_code FROM core.model_routes r"
        " LEFT JOIN core.departments d ON d.company_id = r.company_id AND d.id = r.department_id"
        " ORDER BY r.department_id IS NOT NULL, d.name, r.task_class"
    )).mappings()
    return [RouteView(**r) for r in rows]


@router.get("/models", response_model=list[ModelProfileView])
def api_models(p: Principal = Depends(current_principal)):
    p.require("model.manage")
    with tx(p) as conn:
        return _profiles(conn)


@router.post("/models", response_model=ModelProfileView, status_code=201)
def api_create_model(body: CreateModelProfile, p: Principal = Depends(current_principal)):
    p.require("model.manage")
    try:
        with tx(p) as conn:
            conn.execute(text(
                "INSERT INTO core.model_profiles (company_id, code, display_name, provider, model_name, purpose,"
                " max_output_tokens, temperature, enabled) VALUES (core.current_company_id(), :code, :display_name,"
                " :provider, :model_name, :purpose, :max_output_tokens, :temperature, :enabled)"
            ), body.model_dump())
            audit.record(conn, p.company_id, p.user_id, "model.profile_created", "model_profile", body.code,
                         {"provider": body.provider, "model_name": body.model_name})
            return next(m for m in _profiles(conn) if m.code == body.code)
    except IntegrityError as exc:
        raise Conflict("A model profile with this code already exists.") from exc


@router.patch("/models/{code}", response_model=ModelProfileView)
def api_update_model(code: Code, body: UpdateModelProfile, p: Principal = Depends(current_principal)):
    p.require("model.manage")
    with tx(p) as conn:
        changes = body.model_dump(exclude_unset=True, exclude_none=True)
        if changes:
            if conn.execute(text(f"UPDATE core.model_profiles SET {set_clause(changes)}, updated_at = now()"
                                 " WHERE code = :code RETURNING code"),
                            {**changes, "code": code}).first() is None:
                raise NotFound()
            audit.record(conn, p.company_id, p.user_id, "model.profile_updated", "model_profile", code,
                         {k: str(v) for k, v in changes.items()})
        profile = next((m for m in _profiles(conn) if m.code == code), None)
        if profile is None:
            raise NotFound()
        return profile


@router.get("/model-routes", response_model=list[RouteView])
def api_routes(p: Principal = Depends(current_principal)):
    p.require("model.manage")
    with tx(p) as conn:
        return _routes(conn)


@router.put("/model-routes", response_model=list[RouteView])
def api_set_route(body: SetRoute, p: Principal = Depends(current_principal)):
    p.require("model.manage")
    with tx(p) as conn:
        if body.department_id is not None:
            load_department(conn, body.department_id)
        if conn.execute(text("SELECT 1 FROM core.model_profiles WHERE code = :c"), {"c": body.profile_code}).first() is None:
            raise NotFound()
        conn.execute(text(
            "INSERT INTO core.model_routes (id, company_id, department_id, task_class, profile_code)"
            " VALUES (gen_random_uuid(), core.current_company_id(), :d, :t, :p)"
            " ON CONFLICT (company_id, coalesce(department_id, '00000000-0000-0000-0000-000000000000'::uuid), task_class)"
            " DO UPDATE SET profile_code = EXCLUDED.profile_code, updated_at = now()"
        ), {"d": body.department_id, "t": body.task_class, "p": body.profile_code})
        audit.record(conn, p.company_id, p.user_id, "model.route_set", "model_route", body.task_class,
                     {"department_id": str(body.department_id) if body.department_id else None, "profile": body.profile_code})
        return _routes(conn)


@router.delete("/model-routes", response_model=list[RouteView])
def api_reset_route(department_id: UUID, task_class: Literal["general", "heavy"], p: Principal = Depends(current_principal)):
    """Remove a department override so the company default applies again."""
    p.require("model.manage")
    with tx(p) as conn:
        conn.execute(text("DELETE FROM core.model_routes WHERE department_id = :d AND task_class = :t"),
                     {"d": department_id, "t": task_class})
        audit.record(conn, p.company_id, p.user_id, "model.route_reset", "model_route", task_class,
                     {"department_id": str(department_id)})
        return _routes(conn)


@router.get("/model-providers", response_model=list[ProviderStatus])
def api_providers(p: Principal = Depends(current_principal)):
    p.require("model.manage")
    settings = get_settings()
    installed: list[str] = []
    reachable = False
    try:
        r = httpx.get(f"{settings.ollama_url.rstrip('/')}/api/tags", timeout=3)
        reachable = r.status_code == 200
        installed = sorted(m["name"] for m in r.json().get("models", []))[:100] if reachable else []
    except (httpx.HTTPError, ValueError):
        reachable = False
    return [
        ProviderStatus(provider="ollama", configured=True, reachable=reachable, installed_models=installed),
        ProviderStatus(provider="openai_compatible", configured=bool(settings.openai_compatible_url and settings.openai_compatible_api_key),
                       reachable=None, installed_models=[]),
        ProviderStatus(provider="anthropic", configured=settings.anthropic_api_key is not None, reachable=None, installed_models=[]),
    ]


class _Ping(BaseModel):
    ok: bool


@router.post("/models/{code}/test", response_model=ModelTestResult)
def api_test_model(code: Code, p: Principal = Depends(current_principal)):
    p.require("model.manage")
    with tx(p) as conn:
        row = conn.execute(text("SELECT code, display_name, provider, model_name, temperature, external_ai_enabled"
                                " FROM core.model_profiles, core.company_settings WHERE code = :c"), {"c": code}).first()
    if row is None:
        raise NotFound()
    if row.provider in EXTERNAL_PROVIDERS and not row.external_ai_enabled:
        return ModelTestResult(ok=False, error_code="EXTERNAL_AI_DISABLED")
    profile = gateway.Profile(row.code, row.display_name, row.provider, row.model_name, 64, float(row.temperature))
    try:
        gateway.complete(p, profile, output_model=_Ping, system="Reply with the JSON object {\"ok\": true}.",
                         messages=[ChatMessage("user", "ping")], stage="answer", task_class="general",
                         department_id=None, conversation_id=None, max_tokens=64)
        return ModelTestResult(ok=True, error_code=None)
    except gateway.ModelUnavailable as exc:
        return ModelTestResult(ok=False, error_code=exc.code)


# --- usage tracking -----------------------------------------------------------------
class UsageRow(BaseModel):
    day: datetime
    department_name: str | None
    profile_code: str
    runs: int
    errors: int
    input_tokens: int
    output_tokens: int
    avg_latency_ms: int


@router.get("/usage", response_model=list[UsageRow])
def api_usage(days: int = Query(7, ge=1, le=90), p: Principal = Depends(current_principal)):
    p.require("ai.usage.read")
    with tx(p) as conn:
        rows = conn.execute(text(
            "SELECT date_trunc('day', r.created_at) AS day, d.name AS department_name, r.profile_code,"
            " count(*) AS runs, count(*) FILTER (WHERE r.status <> 'ok') AS errors,"
            " sum(r.input_tokens) AS input_tokens, sum(r.output_tokens) AS output_tokens,"
            " coalesce(avg(r.latency_ms) FILTER (WHERE r.status = 'ok'), 0)::int AS avg_latency_ms"
            " FROM core.ai_runs r LEFT JOIN core.departments d ON d.company_id = r.company_id AND d.id = r.department_id"
            " WHERE r.created_at > now() - make_interval(days => :days)"
            " GROUP BY 1, 2, 3 ORDER BY 1 DESC, 2 NULLS FIRST, 3 LIMIT 2000"
        ), {"days": days}).mappings()
        return [UsageRow(**r) for r in rows]


# --- data inventory and retention ---------------------------------------------------------
class DepartmentData(BaseModel):
    department_id: UUID
    department_name: str
    status: str
    documents: int
    projects: int
    tasks: int
    purchase_requests: int
    index_pending: int
    index_indexed: int
    index_failed: int


class DataInventory(BaseModel):
    departments: list[DepartmentData]
    leave_requests: int
    access_requests: int
    audit_events: int
    ai_runs: int
    conversations_note: str
    chat_retention_days: int | None
    last_purge_at: datetime | None
    embedding_model: str | None
    index_errors: dict[str, int]   # safe error code -> number of current versions that failed with it


class PurgeView(BaseModel):
    retention_days: int | None
    conversations_deleted: int
    runs_pseudonymised: int


@router.get("/data", response_model=DataInventory)
def api_data(p: Principal = Depends(current_principal)):
    p.require("company.settings.manage")
    with tx(p) as conn:
        depts = conn.execute(text(
            "SELECT d.id AS department_id, d.name AS department_name, d.status,"
            " (SELECT count(*) FROM core.documents x WHERE x.department_id = d.id) AS documents,"
            " (SELECT count(*) FROM core.projects x WHERE x.department_id = d.id) AS projects,"
            " (SELECT count(*) FROM core.tasks t JOIN core.projects x ON x.id = t.project_id WHERE x.department_id = d.id) AS tasks,"
            " (SELECT count(*) FROM core.purchase_requests x WHERE x.department_id = d.id) AS purchase_requests,"
            " count(v.id) FILTER (WHERE v.index_status = 'pending') AS index_pending,"
            " count(v.id) FILTER (WHERE v.index_status = 'indexed') AS index_indexed,"
            " count(v.id) FILTER (WHERE v.index_status = 'failed') AS index_failed"
            " FROM core.departments d"
            " LEFT JOIN core.documents doc ON doc.company_id = d.company_id AND doc.department_id = d.id AND doc.status = 'active'"
            " LEFT JOIN core.document_versions v ON v.document_id = doc.id AND v.version = doc.current_version"
            " GROUP BY d.id ORDER BY d.name"
        )).mappings()
        counts = conn.execute(text(
            "SELECT (SELECT count(*) FROM core.leave_requests) AS leave_requests,"
            " (SELECT count(*) FROM core.access_requests) AS access_requests,"
            " (SELECT count(*) FROM core.audit_events) AS audit_events,"
            " (SELECT count(*) FROM core.ai_runs) AS ai_runs,"
            " (SELECT chat_retention_days FROM core.company_settings) AS chat_retention_days,"
            " (SELECT max(occurred_at) FROM core.audit_events WHERE action = 'retention.chats_purged') AS last_purge_at"
        )).mappings().one()
        errors = dict(conn.execute(text(
            "SELECT v.index_error_code, count(*) FROM core.document_versions v JOIN core.documents d"
            " ON d.id = v.document_id AND d.current_version = v.version"
            " WHERE v.index_error_code IS NOT NULL GROUP BY 1"
        )).all())
        embedder = get_embedder()
        return DataInventory(
            departments=[DepartmentData(**d) for d in depts],
            conversations_note="Chats are private to each employee and are not visible in this console.",
            embedding_model=embedder.model if embedder else None,
            index_errors=errors,
            **counts,
        )


class RetryView(BaseModel):
    requeued: int


@router.post("/index/retry", response_model=RetryView)
def api_retry_index(p: Principal = Depends(current_principal)):
    """Put failed document versions back in the indexing queue."""
    p.require("company.settings.manage")
    with tx(p) as conn:
        requeued = retry_failed(conn)
        audit.record(conn, p.company_id, p.user_id, "documents.index_retried", "company", p.company_id,
                     {"requeued": requeued})
        return RetryView(requeued=requeued)


@router.post("/retention/run", response_model=PurgeView)
def api_run_retention(p: Principal = Depends(current_principal)):
    p.require("company.settings.manage")
    result = retention.purge_company(p.company_id, p.user_id)
    return PurgeView(retention_days=result.retention_days, conversations_deleted=result.conversations_deleted,
                     runs_pseudonymised=result.runs_pseudonymised)


# --- audit -------------------------------------------------------------------------
class AuditView(BaseModel):
    id: int
    occurred_at: datetime
    actor_name: str | None
    action: str
    resource_type: str
    resource_id: str | None
    details: dict


@router.get("/audit", response_model=list[AuditView])
def api_audit(
    before_id: int | None = Query(None, ge=1),
    action_prefix: Annotated[str, StringConstraints(pattern=r"^[a-z_.]{1,60}$")] | None = None,
    limit: int = Query(100, ge=1, le=500),
    p: Principal = Depends(current_principal),
):
    p.require("audit.read")
    with tx(p) as conn:
        rows = conn.execute(text(
            "SELECT a.id, a.occurred_at, u.display_name AS actor_name, a.action, a.resource_type, a.resource_id, a.details"
            " FROM core.audit_events a LEFT JOIN core.users u ON u.id = a.actor_user_id"
            " WHERE (CAST(:b AS bigint) IS NULL OR a.id < :b) AND (CAST(:pre AS text) IS NULL OR a.action LIKE :pre || '%')"
            " ORDER BY a.id DESC LIMIT :lim"
        ), {"b": before_id, "pre": action_prefix, "lim": limit}).mappings()
        return [AuditView(**r) for r in rows]


class GraphReadView(BaseModel):
    occurred_at: datetime
    actor_name: str | None
    operation: str
    root_kind: str | None
    root_ref_id: UUID | None
    target_ref_id: UUID | None
    depth: int
    outcome: str
    node_count: int
    request_id: str | None


@router.get("/graph-read-log", response_model=list[GraphReadView])
def api_graph_read_log(
    since: datetime,
    until: datetime,
    root_ref_id: UUID | None = None,
    limit: int = Query(500, ge=1, le=2000),
    p: Principal = Depends(current_principal),
):
    """Incident export (INT-09): who read which graph resource in a time window. Metadata only; no per-person
    statistics are offered anywhere. Every export is itself audited."""
    p.require("audit.read")
    if until <= since:
        raise Invalid("The end of the window must be after its start.")
    with tx(p) as conn:
        rows = conn.execute(text(
            "SELECT g.occurred_at, u.display_name AS actor_name, g.operation, g.root_kind, g.root_ref_id, g.target_ref_id,"
            " g.depth, g.outcome, g.node_count, g.request_id"
            " FROM core.graph_read_log g LEFT JOIN core.users u ON u.id = g.actor_user_id"
            " WHERE g.occurred_at >= :s AND g.occurred_at < :e"
            " AND (CAST(:r AS uuid) IS NULL OR g.root_ref_id = :r OR g.target_ref_id = :r)"
            " ORDER BY g.occurred_at, g.id LIMIT :lim"), {"s": since, "e": until, "r": root_ref_id, "lim": limit}).mappings()
        result = [GraphReadView(**r) for r in rows]
        audit.record(conn, p.company_id, p.user_id, "audit.graph_reads_exported", "company", p.company_id,
                     {"since": since.isoformat(), "until": until.isoformat(), "rows": len(result),
                      "root_ref_id": str(root_ref_id) if root_ref_id else None})
        return result
