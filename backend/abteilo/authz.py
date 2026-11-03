"""Principal and permission checks. Grants come only from role assignments in
the database; nothing the client or the model sends can widen them."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Connection, text

from abteilo.errors import Forbidden


@dataclass(frozen=True)
class Grant:
    permission: str
    department_id: UUID | None  # None = company-wide


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    company_id: UUID
    email: str
    display_name: str
    grants: frozenset[Grant]

    def can(self, permission: str, department_id: UUID | None = None) -> bool:
        """Company-wide grants apply everywhere; department grants only in their department."""
        for g in self.grants:
            if g.permission != permission:
                continue
            if g.department_id is None or g.department_id == department_id:
                return True
        return False

    def require(self, permission: str, department_id: UUID | None = None) -> None:
        if not self.can(permission, department_id):
            raise Forbidden()

    def departments_with(self, permission: str) -> set[UUID]:
        return {g.department_id for g in self.grants if g.permission == permission and g.department_id}

    def has_company_wide(self, permission: str) -> bool:
        return any(g.permission == permission and g.department_id is None for g in self.grants)

    @property
    def member_departments(self) -> set[UUID]:
        return self.departments_with("department.member")


def load_grants(conn: Connection, company_id: UUID, user_id: UUID) -> frozenset[Grant]:
    rows = conn.execute(
        text(
            """
            SELECT DISTINCT rp.permission_code, ra.department_id
            FROM core.role_assignments ra
            JOIN core.role_permissions rp
              ON rp.company_id = ra.company_id AND rp.role_code = ra.role_code
            LEFT JOIN core.departments d
              ON d.company_id = ra.company_id AND d.id = ra.department_id
            WHERE ra.company_id = :c AND ra.user_id = :u AND ra.revoked_at IS NULL
              AND (ra.department_id IS NULL OR d.status = 'active')
            """
        ),
        {"c": company_id, "u": user_id},
    ).all()
    return frozenset(Grant(r[0], r[1]) for r in rows)
