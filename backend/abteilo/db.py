"""Database access. Every unit of work is one short, explicit transaction.

The runtime role is subject to Row-Level Security. Trusted code sets the tenant
and user context with transaction-local settings, so a pooled connection never
carries one request's context into the next.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import Connection, Engine, create_engine, text

from abteilo.config import get_settings

_engine: Engine | None = None


def configure_engine(url: str) -> Engine:
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=5, pool_recycle=1800)
    return _engine


def set_clause(changes: dict) -> str:
    """`a = :a, b = :b` for an UPDATE. Keys come from typed request models, never from raw input; the
    identifier check keeps a future caller from turning a column name into SQL."""
    if not all(key.isidentifier() for key in changes):
        raise ValueError("column names must be identifiers")
    return ", ".join(f"{key} = :{key}" for key in changes)


def get_engine() -> Engine:
    if _engine is None:
        return configure_engine(get_settings().database_url.get_secret_value())
    return _engine


@contextmanager
def transaction(
    company_id: UUID | None = None,
    user_id: UUID | None = None,
    retention_job: bool = False,
) -> Iterator[Connection]:
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "SELECT set_config('app.company_id', :c, true),"
                " set_config('app.user_id', :u, true),"
                " set_config('app.retention_job', :r, true),"
                " set_config('TimeZone', 'UTC', true)"
            ),
            {"c": str(company_id) if company_id else "", "u": str(user_id) if user_id else "", "r": "on" if retention_job else ""},
        )
        yield conn
