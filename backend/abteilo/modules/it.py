"""IT & Security: role catalog and the access-request workflow.

An IT access administrator proposes a role change; a security administrator
(a different person, not the target) approves it. Only an approval changes
role assignments. Revocations are recorded, never deleted.
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends
from pydantic import BaseModel, StringConstraints
from sqlalchemy import Connection, text

from abteilo import audit
from abteilo.authz import Principal
from abteilo.company import assign_role, ensure_active_owner, lock_company
from abteilo.deps import current_principal, load_department, tx
from abteilo.errors import Conflict, Forbidden, Invalid, NotFound

router = APIRouter(prefix="/api", tags=["it"])


class RoleView(BaseModel):
    code: str
    name: str
    scope: Literal["company", "department"]
    elevated: bool
    permissions: list[str]


class AccessRequestView(BaseModel):
    id: UUID
    target_user_id: UUID
    target_name: str
    target_active: bool = True
    role_code: str
    elevated: bool
    department_id: UUID | None
    department_name: str | None
    justification: str
    status: Literal["proposed", "approved", "rejected"]
    proposed_by: UUID
    proposer_name: str
    proposer_active: bool = True
    decided_by: UUID | None
    created_at: datetime
    decided_at: datetime | None


class ProposeAccess(BaseModel):
    target_user_id: UUID
    role_code: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,60}$")]
    department_id: UUID | None = None
    justification: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class AccessDecision(BaseModel):
    decision: Literal["approve", "reject"]


class MemberView(BaseModel):
    user_id: UUID
    display_name: str
    title: str | None


class AssignmentView(BaseModel):
    id: UUID
    user_id: UUID
    display_name: str
    role_code: str
    department_id: UUID | None
    department_name: str | None
    granted_at: datetime


def role_catalog(conn: Connection) -> list[RoleView]:
    rows = conn.execute(
        text("SELECT r.code, r.name, r.scope, r.elevated, coalesce(array_agg(rp.permission_code ORDER BY rp.permission_code)"
             " FILTER (WHERE rp.permission_code IS NOT NULL), '{}') AS permissions"
             " FROM core.roles r LEFT JOIN core.role_permissions rp ON rp.company_id = r.company_id AND rp.role_code = r.code"
             " GROUP BY r.code, r.name, r.scope, r.elevated ORDER BY r.scope, r.code")
    ).mappings()
    return [RoleView(**r) for r in rows]


_AR_SQL = """
SELECT ar.id, ar.target_user_id, t.display_name AS target_name, EXISTS (SELECT 1 FROM core.memberships am JOIN core.users au ON au.id = am.user_id WHERE am.company_id = core.current_company_id() AND am.user_id = ar.target_user_id AND am.status = 'active' AND au.status = 'active') AS target_active, ar.role_code, r.elevated, ar.department_id,
       d.name AS department_name, ar.justification, ar.status, ar.proposed_by, pu.display_name AS proposer_name, EXISTS (SELECT 1 FROM core.memberships am JOIN core.users au ON au.id = am.user_id WHERE am.company_id = core.current_company_id() AND am.user_id = ar.proposed_by AND am.status = 'active' AND au.status = 'active') AS proposer_active,
       ar.decided_by, ar.created_at, ar.decided_at
FROM core.access_requests ar
JOIN core.users t ON t.id = ar.target_user_id
JOIN core.users pu ON pu.id = ar.proposed_by
JOIN core.roles r ON r.company_id = ar.company_id AND r.code = ar.role_code
LEFT JOIN core.departments d ON d.company_id = ar.company_id AND d.id = ar.department_id
WHERE {where} ORDER BY ar.created_at DESC LIMIT 200
"""


def can_view_access(p: Principal, it_department_id: UUID) -> bool:
    return p.can("it.access.propose", it_department_id) or p.has_company_wide("it.access.approve")


def list_access_requests(conn: Connection, p: Principal, it_department_id: UUID, status: str | None) -> list[AccessRequestView]:
    if not can_view_access(p, it_department_id):
        raise Forbidden()
    rows = conn.execute(text(_AR_SQL.format(where="(CAST(:s AS text) IS NULL OR ar.status = :s)")), {"s": status}).mappings()
    return [AccessRequestView(**r) for r in rows]


def _one(conn: Connection, request_id: UUID) -> AccessRequestView:
    row = conn.execute(text(_AR_SQL.format(where="ar.id = :id")), {"id": request_id}).mappings().first()
    if row is None:
        raise NotFound()
    return AccessRequestView(**row)


@router.get("/directory", response_model=list[MemberView])
def api_directory(p: Principal = Depends(current_principal)):
    # Minimal directory for pickers: no emails, no personal details (data minimisation).
    with tx(p) as conn:
        rows = conn.execute(
            text("SELECT m.user_id, u.display_name, m.title FROM core.memberships m JOIN core.users u ON u.id = m.user_id"
                 " WHERE m.company_id = core.current_company_id() AND m.status = 'active' ORDER BY u.display_name LIMIT 1000")
        ).mappings()
        return [MemberView(**r) for r in rows]


@router.get("/it/{department_id}/roles", response_model=list[RoleView])
def api_roles(department_id: UUID, p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        load_department(conn, department_id, "it")
        if not can_view_access(p, department_id):
            raise Forbidden()
        return role_catalog(conn)


@router.get("/it/{department_id}/assignments", response_model=list[AssignmentView])
def api_assignments(department_id: UUID, p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        load_department(conn, department_id, "it")
        if not can_view_access(p, department_id):
            raise Forbidden()
        rows = conn.execute(
            text("SELECT ra.id, ra.user_id, u.display_name, ra.role_code, ra.department_id, d.name AS department_name,"
                 " ra.granted_at FROM core.role_assignments ra JOIN core.users u ON u.id = ra.user_id"
                 " LEFT JOIN core.departments d ON d.company_id = ra.company_id AND d.id = ra.department_id"
                 " WHERE ra.revoked_at IS NULL ORDER BY u.display_name, ra.role_code LIMIT 2000")
        ).mappings()
        return [AssignmentView(**r) for r in rows]


@router.get("/it/{department_id}/access-requests", response_model=list[AccessRequestView])
def api_list_access_requests(department_id: UUID, status: str | None = None, p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        load_department(conn, department_id, "it")
        return list_access_requests(conn, p, department_id, status)


@router.post("/it/{department_id}/access-requests", response_model=AccessRequestView, status_code=201)
def api_propose_access(department_id: UUID, body: ProposeAccess, p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        load_department(conn, department_id, "it")
        p.require("it.access.propose", department_id)
        scope = conn.execute(text("SELECT scope FROM core.roles WHERE code = :r"), {"r": body.role_code}).scalar()
        if scope is None or (scope == "department") != (body.department_id is not None):
            raise Invalid("Department roles need a department; company roles must not have one.")
        if body.department_id is not None:
            load_department(conn, body.department_id)
        request_id = uuid4()
        conn.execute(
            text("INSERT INTO core.access_requests (id, company_id, target_user_id, role_code, department_id,"
                 " justification, proposed_by) VALUES (:id, core.current_company_id(), :t, :r, :d, :j, :u)"),
            {"id": request_id, "t": body.target_user_id, "r": body.role_code, "d": body.department_id,
             "j": body.justification, "u": p.user_id},
        )
        audit.record(conn, p.company_id, p.user_id, "it.access_proposed", "access_request", request_id,
                     {"role": body.role_code, "target": str(body.target_user_id)})
        return _one(conn, request_id)


@router.post("/it/access-requests/{request_id}/decision", response_model=AccessRequestView)
def api_decide_access(request_id: UUID, body: AccessDecision, p: Principal = Depends(current_principal)):
    """Ordinary roles: a security administrator approves. Elevated roles (owner,
    security administrator): only a holder of role.elevated.approve may decide."""
    if not (p.has_company_wide("it.access.approve") or p.has_company_wide("role.elevated.approve")):
        raise Forbidden()
    with tx(p) as conn:
        ar = conn.execute(
            text("SELECT ar.target_user_id, ar.role_code, ar.department_id, ar.status, ar.proposed_by, r.elevated"
                 " FROM core.access_requests ar JOIN core.roles r ON r.company_id = ar.company_id AND r.code = ar.role_code"
                 " WHERE ar.id = :id FOR UPDATE OF ar"),
            {"id": request_id},
        ).first()
        if ar is None:
            raise NotFound()
        required = "role.elevated.approve" if ar.elevated else "it.access.approve"
        if not p.has_company_wide(required):
            raise Forbidden("This role grants company-wide control and needs the owner's approval."
                            if ar.elevated else None)
        if p.user_id in (ar.proposed_by, ar.target_user_id):
            raise Forbidden("A different person must approve this change.")
        if ar.status != "proposed":
            raise Conflict("This request has already been decided.")
        new_status = "approved" if body.decision == "approve" else "rejected"
        conn.execute(
            text("UPDATE core.access_requests SET status = :s, decided_by = :u, decided_at = now() WHERE id = :id"),
            {"s": new_status, "u": p.user_id, "id": request_id},
        )
        if new_status == "approved":
            assign_role(conn, p.company_id, ar.target_user_id, ar.role_code, ar.department_id, p.user_id)
        audit.record(conn, p.company_id, p.user_id, f"it.access_{new_status}", "access_request", request_id)
        return _one(conn, request_id)


def revoke_assignment(conn: Connection, p: Principal, assignment_id: UUID) -> None:
    """Shared by IT and the owner console. Elevated roles need role.elevated.approve;
    ordinary roles need it.access.approve or member.manage. Nobody revokes their own
    roles, and the last company owner can never be removed (no lockout)."""
    lock_company(conn)  # lock order everywhere: company row first, then the rows being changed
    row = conn.execute(
        text("SELECT ra.user_id, ra.role_code, r.elevated FROM core.role_assignments ra"
             " JOIN core.roles r ON r.company_id = ra.company_id AND r.code = ra.role_code"
             " WHERE ra.id = :id AND ra.revoked_at IS NULL FOR UPDATE OF ra"),
        {"id": assignment_id},
    ).first()
    if row is None:
        raise NotFound()
    if row.user_id == p.user_id:
        raise Forbidden("You cannot revoke your own roles.")
    allowed = (p.has_company_wide("role.elevated.approve") if row.elevated
               else p.has_company_wide("it.access.approve") or p.has_company_wide("member.manage"))
    if not allowed:
        raise Forbidden()
    conn.execute(text("UPDATE core.role_assignments SET revoked_at = now(), revoked_by = :u"
                      " WHERE id = :id AND revoked_at IS NULL"),
                 {"id": assignment_id, "u": p.user_id})
    if row.role_code == "company_owner":
        ensure_active_owner(conn)
    # Grants are reloaded on every request, so revocation takes effect immediately.
    audit.record(conn, p.company_id, p.user_id, "it.role_revoked", "role_assignment", assignment_id,
                 {"role": row.role_code, "user": str(row.user_id)})


@router.post("/it/assignments/{assignment_id}/revoke", response_model=AssignmentView)
def api_revoke_assignment(assignment_id: UUID, p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        revoke_assignment(conn, p, assignment_id)
        result = conn.execute(
            text("SELECT ra.id, ra.user_id, u.display_name, ra.role_code, ra.department_id, d.name AS department_name,"
                 " ra.granted_at FROM core.role_assignments ra JOIN core.users u ON u.id = ra.user_id"
                 " LEFT JOIN core.departments d ON d.company_id = ra.company_id AND d.id = ra.department_id"
                 " WHERE ra.id = :id"),
            {"id": assignment_id},
        ).mappings().one()
        return AssignmentView(**result)
