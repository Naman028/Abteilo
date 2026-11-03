"""Invariants enforced by PostgreSQL itself, checked as the real runtime role."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from abteilo import company
from abteilo.db import transaction
from abteilo.errors import Conflict, Invalid
from tests.conftest import PASSWORD, owner_conn, user_id


@pytest.mark.parametrize("table", ["projects", "tasks", "purchase_requests", "documents", "document_versions",
                                   "leave_requests", "departments", "audit_events", "budgets"])
def test_runtime_role_cannot_delete_business_data(demo, table):
    # DATA-01: deleting company business data is impossible for the application role.
    with pytest.raises(ProgrammingError, match="permission denied"):
        with transaction(demo, user_id("owner@bina.example")) as conn:
            conn.execute(text(f"DELETE FROM core.{table}"))


def test_audit_events_are_append_only(demo):
    with pytest.raises(ProgrammingError, match="permission denied"):
        with transaction(demo) as conn:
            conn.execute(text("UPDATE core.audit_events SET action = 'x.y'"))


def test_rls_isolates_tenants_and_fails_closed(demo):
    other = company.create_company(name="Other GmbH", slug="other", industry="retail", owner_email="o@other.example",
                                   owner_name="Other Owner", owner_password=PASSWORD, department_templates=["finance"])
    with transaction(demo) as conn:
        assert conn.execute(text("SELECT count(*) FROM core.departments WHERE company_id = :c"),
                            {"c": other.company_id}).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM core.departments")).scalar_one() == 4
    with transaction() as conn:  # no tenant context
        assert conn.execute(text("SELECT count(*) FROM core.projects")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM core.companies")).scalar_one() == 0


def test_company_role_cannot_be_assigned_to_a_department(demo):
    with owner_conn() as conn:
        dept = conn.execute(text("SELECT id FROM core.departments WHERE company_id = :c LIMIT 1"), {"c": demo}).scalar_one()
    with pytest.raises(IntegrityError):
        with transaction(demo) as conn:
            company.assign_role(conn, demo, user_id("jonas.becker@bina.example"), "company_owner", dept, None)


def test_self_approval_is_rejected_by_the_database(demo):
    sara = user_id("sara.koch@bina.example")
    with pytest.raises(IntegrityError):
        with transaction(demo, sara) as conn:
            conn.execute(text(
                "UPDATE core.purchase_requests SET status = 'approved', decided_by = requested_by, decided_at = now()"
            ))


def test_onboarding_is_atomic(demo):
    with pytest.raises(Invalid):
        company.create_company(name="Bad", slug="bad-co", industry="x", owner_email="bad@x.example", owner_name="Bad",
                               owner_password=PASSWORD, department_templates=["engineering", "unknown"])
    with pytest.raises(Conflict):  # slug collides after the owner user row was inserted in the same transaction
        company.create_company(name="Dup", slug="bina", industry="x", owner_email="dup@x.example", owner_name="Dup",
                               owner_password=PASSWORD, department_templates=["finance"])
    with owner_conn() as conn:
        assert conn.execute(text("SELECT count(*) FROM core.users WHERE email IN ('bad@x.example', 'dup@x.example')")).scalar_one() == 0
