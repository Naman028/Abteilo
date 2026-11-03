"""Finance: budgets and the purchase-request approval workflow.

State machine: submitted -> approved | rejected (finance), submitted -> cancelled (requester).
An approval is refused when it would exceed the department's remaining budget for
the period. The budget row is locked so concurrent approvals cannot overspend.
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import Connection, text

from abteilo import audit
from abteilo.authz import Principal
from abteilo.deps import current_principal, load_department, tx
from abteilo.errors import Conflict, Forbidden, NotFound

router = APIRouter(prefix="/api", tags=["finance"])

Period = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-Q[1-4]$")]
RequestStatus = Literal["submitted", "approved", "rejected", "cancelled"]


class BudgetLine(BaseModel):
    department_id: UUID
    department_name: str
    period: str
    budget_cents: int
    approved_cents: int
    pending_cents: int
    remaining_cents: int


class PurchaseRequestView(BaseModel):
    id: UUID
    department_id: UUID
    department_name: str
    project_id: UUID | None
    title: str
    justification: str
    amount_cents: int
    period: str
    status: RequestStatus
    requested_by: UUID
    requester_name: str
    requester_active: bool = True       # live membership of the requester
    decided_by: UUID | None
    decision_note: str | None
    created_at: datetime
    decided_at: datetime | None


class CreatePurchaseRequest(BaseModel):
    department_id: UUID
    project_id: UUID | None = None
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=200)]
    justification: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    amount_cents: int = Field(gt=0, le=100_000_000_000)
    period: Period


class Decision(BaseModel):
    decision: Literal["approve", "reject"]
    note: Annotated[str, StringConstraints(max_length=2000)] | None = None


class SetBudget(BaseModel):
    department_id: UUID
    period: Period
    amount_cents: int = Field(ge=0, le=100_000_000_000)


def budget_summary(conn: Connection, p: Principal, finance_department_id: UUID, period: str | None) -> list[BudgetLine]:
    p.require("finance.budget.read", finance_department_id)
    rows = conn.execute(
        text(
            """
            SELECT b.department_id, d.name AS department_name, b.period, b.amount_cents AS budget_cents,
                   coalesce(sum(pr.amount_cents) FILTER (WHERE pr.status = 'approved'), 0) AS approved_cents,
                   coalesce(sum(pr.amount_cents) FILTER (WHERE pr.status = 'submitted'), 0) AS pending_cents
            FROM core.budgets b
            JOIN core.departments d ON d.company_id = b.company_id AND d.id = b.department_id
            LEFT JOIN core.purchase_requests pr
              ON pr.company_id = b.company_id AND pr.department_id = b.department_id AND pr.period = b.period
            WHERE (CAST(:period AS text) IS NULL OR b.period = :period)
            GROUP BY b.department_id, d.name, b.period, b.amount_cents
            ORDER BY b.period DESC, d.name LIMIT 200
            """
        ),
        {"period": period},
    ).mappings()
    return [BudgetLine(**r, remaining_cents=r["budget_cents"] - r["approved_cents"]) for r in rows]


_PR_SQL = """
SELECT pr.id, pr.department_id, d.name AS department_name, pr.project_id, pr.title, pr.justification,
       pr.amount_cents, pr.period, pr.status, pr.requested_by, u.display_name AS requester_name,
       EXISTS (SELECT 1 FROM core.memberships am JOIN core.users au ON au.id = am.user_id WHERE am.company_id = core.current_company_id() AND am.user_id = pr.requested_by AND am.status = 'active' AND au.status = 'active') AS requester_active,
       pr.decided_by, pr.decision_note, pr.created_at, pr.decided_at
FROM core.purchase_requests pr
JOIN core.departments d ON d.company_id = pr.company_id AND d.id = pr.department_id
JOIN core.users u ON u.id = pr.requested_by
WHERE {where} ORDER BY pr.created_at DESC LIMIT 200
"""


def list_purchase_requests(
    conn: Connection, p: Principal, finance_department_id: UUID, status: RequestStatus | None
) -> list[PurchaseRequestView]:
    p.require("finance.budget.read", finance_department_id)
    rows = conn.execute(
        text(_PR_SQL.format(where="(CAST(:s AS text) IS NULL OR pr.status = :s)")), {"s": status}
    ).mappings()
    return [PurchaseRequestView(**r) for r in rows]


def _one(conn: Connection, request_id: UUID) -> PurchaseRequestView:
    row = conn.execute(text(_PR_SQL.format(where="pr.id = :id")), {"id": request_id}).mappings().first()
    if row is None:
        raise NotFound()
    return PurchaseRequestView(**row)


@router.post("/purchase-requests", response_model=PurchaseRequestView, status_code=201)
def api_create_purchase_request(body: CreatePurchaseRequest, p: Principal = Depends(current_principal)):
    p.require("purchase.request.create")
    if body.department_id not in p.member_departments:
        raise Forbidden("You can only request purchases for a department you belong to.")
    with tx(p) as conn:
        load_department(conn, body.department_id)
        if body.project_id is not None:
            project_dept = conn.execute(
                text("SELECT department_id FROM core.projects WHERE id = :p"), {"p": body.project_id}
            ).scalar()
            if project_dept != body.department_id:
                raise NotFound()
        request_id = uuid4()
        conn.execute(
            text(
                "INSERT INTO core.purchase_requests (id, company_id, department_id, project_id, title, justification,"
                " amount_cents, period, requested_by) VALUES (:id, core.current_company_id(), :d, :proj, :t, :j, :a, :per, :u)"
            ),
            {"id": request_id, "d": body.department_id, "proj": body.project_id, "t": body.title,
             "j": body.justification, "a": body.amount_cents, "per": body.period, "u": p.user_id},
        )
        audit.record(conn, p.company_id, p.user_id, "finance.purchase_submitted", "purchase_request", request_id,
                     {"amount_cents": body.amount_cents})
        return _one(conn, request_id)


@router.get("/purchase-requests/mine", response_model=list[PurchaseRequestView])
def api_my_purchase_requests(p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        rows = conn.execute(text(_PR_SQL.format(where="pr.requested_by = :u")), {"u": p.user_id}).mappings()
        return [PurchaseRequestView(**r) for r in rows]


@router.post("/purchase-requests/{request_id}/cancel", response_model=PurchaseRequestView)
def api_cancel_purchase_request(request_id: UUID, p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        updated = conn.execute(
            text(
                "UPDATE core.purchase_requests SET status = 'cancelled'"
                " WHERE id = :id AND requested_by = :u AND status = 'submitted' RETURNING id"
            ),
            {"id": request_id, "u": p.user_id},
        ).first()
        if updated is None:
            raise Conflict("Only your own submitted requests can be cancelled.")
        audit.record(conn, p.company_id, p.user_id, "finance.purchase_cancelled", "purchase_request", request_id)
        return _one(conn, request_id)


@router.get("/finance/{department_id}/budgets", response_model=list[BudgetLine])
def api_budgets(department_id: UUID, period: Period | None = None, p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        load_department(conn, department_id, "finance")
        return budget_summary(conn, p, department_id, period)


@router.put("/finance/{department_id}/budgets", response_model=list[BudgetLine])
def api_set_budget(department_id: UUID, body: SetBudget, p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        load_department(conn, department_id, "finance")
        p.require("finance.budget.manage", department_id)
        load_department(conn, body.department_id)
        conn.execute(
            text(
                "INSERT INTO core.budgets (id, company_id, department_id, period, amount_cents)"
                " VALUES (:id, core.current_company_id(), :d, :per, :a)"
                " ON CONFLICT (company_id, department_id, period) DO UPDATE SET amount_cents = EXCLUDED.amount_cents"
            ),
            {"id": uuid4(), "d": body.department_id, "per": body.period, "a": body.amount_cents},
        )
        audit.record(conn, p.company_id, p.user_id, "finance.budget_set", "department", body.department_id,
                     {"period": body.period, "amount_cents": body.amount_cents})
        return budget_summary(conn, p, department_id, body.period)


@router.get("/finance/{department_id}/purchase-requests", response_model=list[PurchaseRequestView])
def api_list_purchase_requests(
    department_id: UUID, status: RequestStatus | None = Query(None), p: Principal = Depends(current_principal)
):
    with tx(p) as conn:
        load_department(conn, department_id, "finance")
        return list_purchase_requests(conn, p, department_id, status)


@router.post("/finance/{department_id}/purchase-requests/{request_id}/decision", response_model=PurchaseRequestView)
def api_decide_purchase_request(
    department_id: UUID, request_id: UUID, body: Decision, p: Principal = Depends(current_principal)
):
    with tx(p) as conn:
        load_department(conn, department_id, "finance")
        p.require("finance.purchase.approve", department_id)
        pr = conn.execute(
            text("SELECT department_id, period, amount_cents, status, requested_by FROM core.purchase_requests"
                 " WHERE id = :id FOR UPDATE"),
            {"id": request_id},
        ).first()
        if pr is None:
            raise NotFound()
        if pr.requested_by == p.user_id:
            raise Forbidden("You cannot decide your own request.")
        if pr.status != "submitted":
            raise Conflict("This request has already been decided.")
        if body.decision == "approve":
            budget = conn.execute(
                text("SELECT amount_cents FROM core.budgets WHERE department_id = :d AND period = :per FOR UPDATE"),
                {"d": pr.department_id, "per": pr.period},
            ).scalar()
            approved = conn.execute(
                text("SELECT coalesce(sum(amount_cents), 0) FROM core.purchase_requests"
                     " WHERE department_id = :d AND period = :per AND status = 'approved'"),
                {"d": pr.department_id, "per": pr.period},
            ).scalar_one()
            if budget is None or approved + pr.amount_cents > budget:
                raise Conflict("Approving this request would exceed the department budget for the period.")
        new_status = "approved" if body.decision == "approve" else "rejected"
        conn.execute(
            text("UPDATE core.purchase_requests SET status = :s, decided_by = :u, decided_at = now(), decision_note = :n"
                 " WHERE id = :id"),
            {"s": new_status, "u": p.user_id, "n": body.note, "id": request_id},
        )
        audit.record(conn, p.company_id, p.user_id, f"finance.purchase_{new_status}", "purchase_request", request_id)
        return _one(conn, request_id)
