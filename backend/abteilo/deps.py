"""Request dependencies shared by every router."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

from fastapi import Request
from sqlalchemy import Connection, text

from abteilo.authz import Principal
from abteilo.db import transaction
from abteilo.errors import CsrfRequired, NotFound
from abteilo.security import resolve_session

SESSION_COOKIE = "abteilo_session"
CSRF_HEADER = "x-abteilo-csrf"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def current_principal(request: Request) -> Principal:
    # SameSite=Strict cookies plus a mandatory custom header on writes block
    # cross-site request forgery: browsers cannot add the header cross-origin
    # without a CORS preflight, and no CORS origins are allowed.
    if request.method not in _SAFE_METHODS and request.headers.get(CSRF_HEADER) != "1":
        raise CsrfRequired()
    return resolve_session(request.cookies.get(SESSION_COOKIE))


@contextmanager
def tx(p: Principal) -> Iterator[Connection]:
    with transaction(p.company_id, p.user_id) as conn:
        yield conn


@dataclass(frozen=True)
class Department:
    id: UUID
    code: str
    name: str
    template: str
    modules: tuple[str, ...]


def load_department(conn: Connection, department_id: UUID, module: str | None = None) -> Department:
    row = conn.execute(
        text(
            "SELECT d.id, d.code, d.name, d.template,"
            " coalesce(array_agg(m.module_code ORDER BY m.module_code) FILTER (WHERE m.module_code IS NOT NULL), '{}')"
            " FROM core.departments d LEFT JOIN core.department_modules m"
            "   ON m.company_id = d.company_id AND m.department_id = d.id"
            " WHERE d.id = :d AND d.status = 'active' GROUP BY d.id"
        ),
        {"d": department_id},
    ).first()
    if row is None or (module is not None and module not in row[4]):
        raise NotFound()
    return Department(row[0], row[1], row[2], row[3], tuple(row[4]))
