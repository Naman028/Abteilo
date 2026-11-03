"""Company onboarding and department lifecycle."""

import re
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from abteilo import audit
from abteilo.db import transaction
from abteilo.errors import Conflict, Invalid
from abteilo.security import hash_password
from abteilo.templates import (
    DEFAULT_ROUTES,
    DEPARTMENT_TEMPLATES,
    MODEL_TEMPLATES,
    ROLE_TEMPLATES,
)


@dataclass(frozen=True)
class CreatedCompany:
    company_id: UUID
    owner_user_id: UUID
    department_ids: dict[str, UUID]


def create_company(
    *,
    name: str,
    slug: str,
    industry: str,
    owner_email: str,
    owner_name: str,
    owner_password: str,
    department_templates: list[str],
) -> CreatedCompany:
    """One transaction: the company exists completely or not at all."""
    unknown = [t for t in department_templates if t not in DEPARTMENT_TEMPLATES or t == "custom"]
    if unknown or len(set(department_templates)) != len(department_templates):
        raise Invalid("Unknown or duplicate department template.")

    company_id, owner_id = uuid4(), uuid4()
    try:
        with transaction(company_id, owner_id) as conn:
            conn.execute(
                text("INSERT INTO core.users (id, email, display_name, password_hash) VALUES (:id, :e, :n, :p)"),
                {"id": owner_id, "e": owner_email.strip().lower(), "n": owner_name, "p": hash_password(owner_password)},
            )
            conn.execute(
                text("INSERT INTO core.companies (id, name, slug, industry) VALUES (:id, :n, :s, :i)"),
                {"id": company_id, "n": name, "s": slug, "i": industry},
            )
            conn.execute(text("INSERT INTO core.company_settings (company_id) VALUES (:c)"), {"c": company_id})
            conn.execute(
                text("INSERT INTO core.memberships (company_id, user_id, title) VALUES (:c, :u, 'Owner')"),
                {"c": company_id, "u": owner_id},
            )
            _seed_roles(conn, company_id)
            departments = {
                code: _insert_department(conn, company_id, code, DEPARTMENT_TEMPLATES[code].name, code)
                for code in department_templates
            }
            for role in ("company_owner", "employee"):
                assign_role(conn, company_id, owner_id, role, None, granted_by=None)
            _seed_models(conn, company_id, departments)
            audit.record(conn, company_id, owner_id, "company.created", "company", company_id,
                         {"departments": department_templates})
    except IntegrityError as exc:
        raise Conflict("A company or user with these details already exists.") from exc
    return CreatedCompany(company_id, owner_id, departments)


def _seed_roles(conn: Connection, company_id: UUID) -> None:
    for role in ROLE_TEMPLATES:
        conn.execute(
            text("INSERT INTO core.roles (company_id, code, name, scope, elevated) VALUES (:c, :code, :n, :s, :e)"),
            {"c": company_id, "code": role.code, "n": role.name, "s": role.scope, "e": role.elevated},
        )
        for perm in role.permissions:
            conn.execute(
                text("INSERT INTO core.role_permissions (company_id, role_code, permission_code) VALUES (:c, :r, :p)"),
                {"c": company_id, "r": role.code, "p": perm},
            )


def _seed_models(conn: Connection, company_id: UUID, departments: dict[str, UUID]) -> None:
    for m in MODEL_TEMPLATES:
        conn.execute(
            text(
                "INSERT INTO core.model_profiles (company_id, code, display_name, provider, model_name, purpose,"
                " max_output_tokens, enabled) VALUES (:c, :code, :dn, :p, :m, :pu, :mo, :en)"
            ),
            {"c": company_id, "code": m.code, "dn": m.display_name, "p": m.provider, "m": m.model_name,
             "pu": m.purpose, "mo": m.max_output_tokens, "en": m.enabled},
        )
    for dept_code, task_class, profile in DEFAULT_ROUTES:
        if dept_code is not None and dept_code not in departments:
            continue
        conn.execute(
            text(
                "INSERT INTO core.model_routes (id, company_id, department_id, task_class, profile_code)"
                " VALUES (:id, :c, :d, :t, :p)"
            ),
            {"id": uuid4(), "c": company_id, "d": departments.get(dept_code) if dept_code else None,
             "t": task_class, "p": profile},
        )


def _insert_department(conn: Connection, company_id: UUID, code: str, name: str, template: str) -> UUID:
    dept_id = uuid4()
    conn.execute(
        text("INSERT INTO core.departments (id, company_id, code, name, template) VALUES (:id, :c, :code, :n, :t)"),
        {"id": dept_id, "c": company_id, "code": code, "n": name, "t": template},
    )
    for module in DEPARTMENT_TEMPLATES[template].modules:
        conn.execute(
            text("INSERT INTO core.department_modules (company_id, department_id, module_code) VALUES (:c, :d, :m)"),
            {"c": company_id, "d": dept_id, "m": module},
        )
    return dept_id


_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


def add_department(conn: Connection, company_id: UUID, actor: UUID, code: str, name: str, template: str) -> UUID:
    if template not in DEPARTMENT_TEMPLATES or not _CODE_RE.fullmatch(code):
        raise Invalid("Unknown template or invalid department code.")
    if template != "custom" and template != code:
        raise Invalid("Built-in templates use their own code; use the custom template for new departments.")
    exists = conn.execute(
        text("SELECT 1 FROM core.departments WHERE company_id = :c AND code = :code"), {"c": company_id, "code": code}
    ).first()
    if exists:
        raise Conflict("A department with this code already exists.")
    dept_id = _insert_department(conn, company_id, code, name, template)
    audit.record(conn, company_id, actor, "department.created", "department", dept_id, {"code": code, "template": template})
    return dept_id


def lock_company(conn: Connection) -> None:
    """Serialise every operation that can change who owns the company.

    Each such operation (disabling a member, revoking an owner role) first takes
    this row lock, then makes its change, then calls ensure_active_owner(). Two
    concurrent operations therefore run one after the other, and the second one
    sees the first one's committed result before it checks the invariant.
    """
    conn.execute(text("SELECT 1 FROM core.companies WHERE id = core.current_company_id() FOR UPDATE"))


def ensure_active_owner(conn: Connection) -> None:
    """Invariant OWN-01: an active company keeps at least one active owner."""
    owners = conn.execute(text(
        "SELECT count(*) FROM core.role_assignments ra JOIN core.memberships m"
        " ON m.company_id = ra.company_id AND m.user_id = ra.user_id"
        " JOIN core.users u ON u.id = ra.user_id"
        " WHERE ra.role_code = 'company_owner' AND ra.revoked_at IS NULL AND m.status = 'active' AND u.status = 'active'"
    )).scalar_one()
    if owners == 0:
        raise Conflict("The company must keep at least one active owner.")


def assign_role(
    conn: Connection, company_id: UUID, user_id: UUID, role_code: str, department_id: UUID | None, granted_by: UUID | None
) -> None:
    conn.execute(
        text(
            "INSERT INTO core.role_assignments (id, company_id, user_id, role_code, department_id, granted_by)"
            " VALUES (:id, :c, :u, :r, :d, :g) ON CONFLICT DO NOTHING"
        ),
        {"id": uuid4(), "c": company_id, "u": user_id, "r": role_code, "d": department_id, "g": granted_by},
    )


def add_member(
    conn: Connection, company_id: UUID, actor: UUID | None, *, email: str, display_name: str, password: str, title: str | None
) -> UUID:
    user_id = uuid4()
    conn.execute(
        text("INSERT INTO core.users (id, email, display_name, password_hash) VALUES (:id, :e, :n, :p)"),
        {"id": user_id, "e": email.strip().lower(), "n": display_name, "p": hash_password(password)},
    )
    conn.execute(
        text("INSERT INTO core.memberships (company_id, user_id, title) VALUES (:c, :u, :t)"),
        {"c": company_id, "u": user_id, "t": title},
    )
    assign_role(conn, company_id, user_id, "employee", None, actor)
    audit.record(conn, company_id, actor, "member.added", "user", user_id)
    return user_id
