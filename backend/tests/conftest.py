"""Tests run against real PostgreSQL: the runtime role, RLS, grants and
constraints are part of what is being tested. SQLite cannot stand in.

Required: ABTEILO_TEST_DATABASE_URL (runtime role) and
ABTEILO_TEST_MIGRATION_DATABASE_URL (schema owner), via environment or .env.
"""

import os
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import pytest

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def _env(name: str) -> str:
    if name in os.environ:
        return os.environ[name]
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
    pytest.exit(f"{name} is required for the test suite", returncode=2)


RUNTIME_URL = _env("ABTEILO_TEST_DATABASE_URL")
OWNER_URL = _env("ABTEILO_TEST_MIGRATION_DATABASE_URL")
os.environ.update(ABTEILO_ENVIRONMENT="test", ABTEILO_DATABASE_URL=RUNTIME_URL, ABTEILO_COOKIE_SECURE="false",
                  ABTEILO_DEMO_PASSWORD="test-password-123",
                  # Tests never call a real embedding model; RAG tests install a deterministic one.
                  ABTEILO_EMBEDDING_MODEL="")
PASSWORD = "test-password-123"

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from abteilo.ai.providers import PROVIDERS  # noqa: E402
from abteilo.config import get_settings  # noqa: E402
from abteilo.db import configure_engine  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]


def _alembic(action: str, target: str | None = None) -> None:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.attributes["url"] = OWNER_URL
    getattr(command, action)(cfg, target or ("base" if action == "downgrade" else "head"))


@pytest.fixture(scope="session", autouse=True)
def database():
    get_settings.cache_clear()
    # The disposable test database starts clean: rows left by the previous session (report-stage AI runs,
    # quarantined graph rows) block downgrades by design, as they would in a real database.
    owner = create_engine(OWNER_URL)
    with owner.begin() as conn:
        # Inspection through owner_conn relies on FORCE ROW LEVEL SECURITY; a role that bypasses RLS would let
        # tenant-isolation assertions see every company and fail (or pass) for the wrong reason.
        if conn.execute(text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")).scalar():
            pytest.exit("ABTEILO_TEST_MIGRATION_DATABASE_URL must use a schema owner that is neither superuser "
                        "nor BYPASSRLS", returncode=2)
        if conn.execute(text("SELECT to_regclass('core.companies')")).scalar():
            conn.execute(text("TRUNCATE core.companies, core.users CASCADE"))
    owner.dispose()
    _alembic("downgrade")
    _alembic("upgrade")
    configure_engine(RUNTIME_URL)
    yield


@pytest.fixture()
def demo(database):
    """A fresh demo company per test, plus a scripted model routed as default."""
    owner = create_engine(OWNER_URL)
    with owner.begin() as conn:
        conn.execute(text("TRUNCATE core.companies, core.users, conv.conversations, core.login_failures CASCADE"))
    owner.dispose()
    from abteilo.seed import seed

    company_id = seed(PASSWORD)
    _current_company[0] = company_id
    with owner_conn() as conn:
        conn.execute(text(
            "INSERT INTO core.model_profiles (company_id, code, display_name, provider, model_name)"
            " VALUES (:c, 'scripted', 'Scripted test model', 'scripted', 'scripted')"), {"c": company_id})
        conn.execute(text("UPDATE core.model_routes SET profile_code = 'scripted' WHERE company_id = :c"), {"c": company_id})
    scripted = PROVIDERS["scripted"]
    scripted.replies.clear()
    scripted.calls.clear()
    yield company_id


_current_company: list = [None]


@contextmanager
def owner_conn(user_id=None):
    """Schema-owner connection for test setup and inspection. FORCE ROW LEVEL SECURITY
    applies to the owner too, so it runs inside the current demo company's context."""
    engine = create_engine(OWNER_URL)
    try:
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.company_id', :c, true), set_config('app.user_id', :u, true),"
                              " set_config('app.retention_job', 'on', true)"),
                         {"c": str(_current_company[0] or ""), "u": str(user_id or "")})
            yield conn
    finally:
        engine.dispose()


@pytest.fixture()
def app():
    from abteilo.main import create_app

    return create_app()


@pytest.fixture()
def login(app, demo):
    def _login(email: str) -> TestClient:
        client = TestClient(app)
        client.headers["X-Abteilo-CSRF"] = "1"
        r = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
        assert r.status_code == 200, r.text
        client.me = r.json()
        return client

    return _login


def department_id(client: TestClient, name: str) -> str:
    return next(d["id"] for d in client.me["departments"] if d["name"] == name)


def user_id(email: str) -> UUID:
    with owner_conn() as conn:
        return conn.execute(text("SELECT id FROM core.users WHERE email = :e"), {"e": email}).scalar_one()
