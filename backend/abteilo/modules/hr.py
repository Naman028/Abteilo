"""Human resources: leave balances and leave requests.

Employees see only their own leave. HR specialists in the HR department decide
requests; an approval may not exceed the employee's remaining balance.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import Connection, text

from abteilo import audit
from abteilo.authz import Principal
from abteilo.deps import current_principal, load_department, tx
from abteilo.errors import Conflict, Forbidden, Invalid, NotFound

router = APIRouter(prefix="/api/hr", tags=["hr"])

LeaveStatus = Literal["submitted", "approved", "rejected", "cancelled"]
MAX_LEAVE_SPAN_DAYS = 90


class LeaveRequestView(BaseModel):
    id: UUID
    user_id: UUID
    employee_name: str
    employee_active: bool = True        # live membership of the employee
    start_date: date
    end_date: date
    days: Decimal
    status: LeaveStatus
    created_at: datetime
    decided_at: datetime | None


class LeaveSummary(BaseModel):
    year: int
    days_total: Decimal
    days_approved: Decimal
    days_pending: Decimal
    days_remaining: Decimal
    requests: list[LeaveRequestView]


class CreateLeaveRequest(BaseModel):
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def _span(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date before start_date")
        if (self.end_date - self.start_date).days > MAX_LEAVE_SPAN_DAYS or self.start_date.year != self.end_date.year:
            raise ValueError("leave must stay within one year and 90 days")
        return self


class LeaveDecision(BaseModel):
    decision: Literal["approve", "reject"]


class SetBalance(BaseModel):
    user_id: UUID
    year: int = Field(ge=2000, le=2100)
    days_total: Decimal = Field(ge=0, le=100, decimal_places=1)


def business_days(start: date, end: date) -> int:
    return sum(1 for i in range((end - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)


_LR_SQL = """
SELECT lr.id, lr.user_id, u.display_name AS employee_name, EXISTS (SELECT 1 FROM core.memberships am JOIN core.users au ON au.id = am.user_id WHERE am.company_id = core.current_company_id() AND am.user_id = lr.user_id AND am.status = 'active' AND au.status = 'active') AS employee_active, lr.start_date, lr.end_date, lr.days,
       lr.status, lr.created_at, lr.decided_at
FROM core.leave_requests lr JOIN core.users u ON u.id = lr.user_id
WHERE {where} ORDER BY lr.start_date DESC LIMIT 200
"""


def leave_summary(conn: Connection, user_id: UUID, year: int) -> LeaveSummary:
    total = conn.execute(
        text("SELECT days_total FROM core.leave_balances WHERE user_id = :u AND year = :y"), {"u": user_id, "y": year}
    ).scalar() or Decimal(0)
    requests = [
        LeaveRequestView(**r)
        for r in conn.execute(
            text(_LR_SQL.format(where="lr.user_id = :u AND extract(year FROM lr.start_date) = :y")), {"u": user_id, "y": year}
        ).mappings()
    ]
    approved = sum((r.days for r in requests if r.status == "approved"), Decimal(0))
    pending = sum((r.days for r in requests if r.status == "submitted"), Decimal(0))
    return LeaveSummary(year=year, days_total=total, days_approved=approved, days_pending=pending,
                        days_remaining=total - approved, requests=requests)


def pending_leave(conn: Connection, p: Principal, hr_department_id: UUID) -> list[LeaveRequestView]:
    p.require("hr.leave.approve", hr_department_id)
    rows = conn.execute(text(_LR_SQL.format(where="lr.status = 'submitted'"))).mappings()
    return [LeaveRequestView(**r) for r in rows]


@router.get("/me/leave", response_model=LeaveSummary)
def api_my_leave(year: int | None = Query(None, ge=2000, le=2100), p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        return leave_summary(conn, p.user_id, year or date.today().year)


@router.post("/leave-requests", response_model=LeaveRequestView, status_code=201)
def api_request_leave(body: CreateLeaveRequest, p: Principal = Depends(current_principal)):
    p.require("leave.request.create")
    days = business_days(body.start_date, body.end_date)
    if days == 0:
        raise Invalid("The selected range has no working days.")
    with tx(p) as conn:
        overlap = conn.execute(
            text("SELECT 1 FROM core.leave_requests WHERE user_id = :u AND status IN ('submitted', 'approved')"
                 " AND start_date <= :e AND end_date >= :s"),
            {"u": p.user_id, "s": body.start_date, "e": body.end_date},
        ).first()
        if overlap:
            raise Conflict("This overlaps an existing leave request.")
        request_id = uuid4()
        conn.execute(
            text("INSERT INTO core.leave_requests (id, company_id, user_id, start_date, end_date, days)"
                 " VALUES (:id, core.current_company_id(), :u, :s, :e, :d)"),
            {"id": request_id, "u": p.user_id, "s": body.start_date, "e": body.end_date, "d": days},
        )
        audit.record(conn, p.company_id, p.user_id, "hr.leave_submitted", "leave_request", request_id, {"days": days})
        return LeaveRequestView(**conn.execute(text(_LR_SQL.format(where="lr.id = :id")), {"id": request_id}).mappings().one())


@router.post("/leave-requests/{request_id}/cancel", response_model=LeaveRequestView)
def api_cancel_leave(request_id: UUID, p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        if conn.execute(
            text("UPDATE core.leave_requests SET status = 'cancelled' WHERE id = :id AND user_id = :u"
                 " AND status = 'submitted' RETURNING id"),
            {"id": request_id, "u": p.user_id},
        ).first() is None:
            raise Conflict("Only your own submitted requests can be cancelled.")
        audit.record(conn, p.company_id, p.user_id, "hr.leave_cancelled", "leave_request", request_id)
        return LeaveRequestView(**conn.execute(text(_LR_SQL.format(where="lr.id = :id")), {"id": request_id}).mappings().one())


@router.get("/{department_id}/leave-requests", response_model=list[LeaveRequestView])
def api_pending_leave(department_id: UUID, p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        load_department(conn, department_id, "hr")
        return pending_leave(conn, p, department_id)


@router.post("/{department_id}/leave-requests/{request_id}/decision", response_model=LeaveRequestView)
def api_decide_leave(department_id: UUID, request_id: UUID, body: LeaveDecision, p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        load_department(conn, department_id, "hr")
        p.require("hr.leave.approve", department_id)
        lr = conn.execute(
            text("SELECT user_id, start_date, days, status FROM core.leave_requests WHERE id = :id FOR UPDATE"),
            {"id": request_id},
        ).first()
        if lr is None:
            raise NotFound()
        if lr.user_id == p.user_id:
            raise Forbidden("You cannot decide your own leave.")
        if lr.status != "submitted":
            raise Conflict("This request has already been decided.")
        if body.decision == "approve":
            conn.execute(
                text("SELECT 1 FROM core.leave_balances WHERE user_id = :u AND year = :y FOR UPDATE"),
                {"u": lr.user_id, "y": lr.start_date.year},
            )
            if leave_summary(conn, lr.user_id, lr.start_date.year).days_remaining < lr.days:
                raise Conflict("Approving this request would exceed the employee's leave balance.")
        new_status = "approved" if body.decision == "approve" else "rejected"
        conn.execute(
            text("UPDATE core.leave_requests SET status = :s, decided_by = :u, decided_at = now() WHERE id = :id"),
            {"s": new_status, "u": p.user_id, "id": request_id},
        )
        audit.record(conn, p.company_id, p.user_id, f"hr.leave_{new_status}", "leave_request", request_id)
        return LeaveRequestView(**conn.execute(text(_LR_SQL.format(where="lr.id = :id")), {"id": request_id}).mappings().one())


@router.put("/{department_id}/leave-balances", response_model=LeaveSummary)
def api_set_balance(department_id: UUID, body: SetBalance, p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        load_department(conn, department_id, "hr")
        p.require("hr.employee.read", department_id)
        p.require("hr.leave.approve", department_id)
        if not conn.execute(
            text("SELECT 1 FROM core.memberships WHERE company_id = core.current_company_id() AND user_id = :u"),
            {"u": body.user_id},
        ).first():
            raise NotFound()
        conn.execute(
            text("INSERT INTO core.leave_balances (company_id, user_id, year, days_total)"
                 " VALUES (core.current_company_id(), :u, :y, :d)"
                 " ON CONFLICT (company_id, user_id, year) DO UPDATE SET days_total = EXCLUDED.days_total"),
            {"u": body.user_id, "y": body.year, "d": body.days_total},
        )
        audit.record(conn, p.company_id, p.user_id, "hr.balance_set", "user", body.user_id,
                     {"year": body.year, "days_total": str(body.days_total)})
        return leave_summary(conn, body.user_id, body.year)
