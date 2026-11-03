"""Password hashing, login throttling and server-side sessions."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import Connection, text

from abteilo import audit
from abteilo.authz import Principal, load_grants
from abteilo.config import get_settings
from abteilo.db import transaction
from abteilo.errors import AuthRequired, Invalid, InvalidCredentials, RateLimited

_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash("timing-equaliser-not-a-password")

MIN_PASSWORD_LENGTH = 12


def hash_password(password: str) -> str:
    if not (MIN_PASSWORD_LENGTH <= len(password) <= 256):
        raise ValueError("password length out of bounds")
    return _hasher.hash(password)


def _verify(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def _email_hash(email: str) -> bytes:
    return hashlib.sha256(email.strip().lower().encode()).digest()


def _token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


@dataclass(frozen=True)
class NewSession:
    token: str
    expires_at: datetime
    company_id: UUID
    user_id: UUID


def login(email: str, password: str, company_slug: str | None = None) -> NewSession:
    settings = get_settings()
    email = email.strip().lower()
    window_start = datetime.now(UTC) - timedelta(minutes=settings.login_window_minutes)
    with transaction() as conn:
        failures = conn.execute(
            text("SELECT count(*) FROM core.login_failures WHERE email_hash = :h AND occurred_at > :t"),
            {"h": _email_hash(email), "t": window_start},
        ).scalar_one()
        if failures >= settings.login_max_failures:
            raise RateLimited()
        user = conn.execute(
            text("SELECT id, password_hash, status FROM core.users WHERE email = :e"), {"e": email}
        ).first()
        valid = _verify(user.password_hash if user else _DUMMY_HASH, password)
        company_id = _active_company(conn, user.id, company_slug) if user and valid and user.status == "active" else None
        if company_id is None:
            conn.execute(text("INSERT INTO core.login_failures (email_hash) VALUES (:h)"), {"h": _email_hash(email)})
            # Prune old throttle rows opportunistically; they are not business data.
            conn.execute(text("DELETE FROM core.login_failures WHERE occurred_at < :t"), {"t": window_start})
    if company_id is None:
        raise InvalidCredentials()

    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(hours=settings.session_ttl_hours)
    with transaction(company_id, user.id) as conn:
        conn.execute(
            text("INSERT INTO core.sessions (token_hash, user_id, company_id, expires_at) VALUES (:h, :u, :c, :x)"),
            {"h": _token_hash(token), "u": user.id, "c": company_id, "x": expires_at},
        )
        conn.execute(text("UPDATE core.users SET last_login_at = now() WHERE id = :u"), {"u": user.id})
        conn.execute(text("DELETE FROM core.login_failures WHERE email_hash = :h"), {"h": _email_hash(email)})
        audit.record(conn, company_id, user.id, "auth.login", "user", user.id)
    return NewSession(token, expires_at, company_id, user.id)


def _active_company(conn: Connection, user_id: UUID, company_slug: str | None) -> UUID | None:
    candidates = conn.execute(
        text("SELECT company_id FROM core.memberships WHERE user_id = :u AND status = 'active' ORDER BY joined_at"),
        {"u": user_id},
    ).scalars().all()
    if company_slug is None:
        return candidates[0] if candidates else None
    # core.companies is tenant-scoped: read each candidate's slug under its own context.
    for cid in candidates:
        conn.execute(text("SELECT set_config('app.company_id', :c, true)"), {"c": str(cid)})
        if conn.execute(text("SELECT slug FROM core.companies WHERE id = :c"), {"c": cid}).scalar() == company_slug:
            return cid
    return None


def resolve_session(token: str | None) -> Principal:
    if not token or len(token) > 128:
        raise AuthRequired()
    with transaction() as conn:
        row = conn.execute(
            text(
                "SELECT s.user_id, s.company_id FROM core.sessions s"
                " JOIN core.users u ON u.id = s.user_id"
                " JOIN core.memberships m ON m.company_id = s.company_id AND m.user_id = s.user_id"
                " WHERE s.token_hash = :h AND s.revoked_at IS NULL AND s.expires_at > now()"
                " AND u.status = 'active' AND m.status = 'active'"
            ),
            {"h": _token_hash(token)},
        ).first()
    if row is None:
        raise AuthRequired()
    with transaction(row.company_id, row.user_id) as conn:
        user = conn.execute(
            text("SELECT email, display_name FROM core.users WHERE id = :u"), {"u": row.user_id}
        ).one()
        company_ok = conn.execute(
            text("SELECT status = 'active' FROM core.companies WHERE id = :c"), {"c": row.company_id}
        ).scalar()
        if not company_ok:
            raise AuthRequired()
        grants = load_grants(conn, row.company_id, row.user_id)
    return Principal(row.user_id, row.company_id, user.email, user.display_name, grants)


def revoke_session(token: str) -> None:
    with transaction() as conn:
        conn.execute(
            text("UPDATE core.sessions SET revoked_at = now() WHERE token_hash = :h AND revoked_at IS NULL"),
            {"h": _token_hash(token)},
        )


def change_own_password(p: Principal, session_token: str, current_password: str, new_password: str) -> None:
    """Self-service change. A wrong current password counts toward the login throttle (a stolen session must
    not become a password-guessing oracle). Every other session of the user is signed out; this one stays."""
    settings = get_settings()
    window_start = datetime.now(UTC) - timedelta(minutes=settings.login_window_minutes)
    with transaction(p.company_id, p.user_id) as conn:
        failures = conn.execute(
            text("SELECT count(*) FROM core.login_failures WHERE email_hash = :h AND occurred_at > :t"),
            {"h": _email_hash(p.email), "t": window_start},
        ).scalar_one()
        if failures >= settings.login_max_failures:
            raise RateLimited()
        stored = conn.execute(text("SELECT password_hash FROM core.users WHERE id = :u"), {"u": p.user_id}).scalar_one()
        valid = _verify(stored, current_password)
        if not valid:
            conn.execute(text("INSERT INTO core.login_failures (email_hash) VALUES (:h)"), {"h": _email_hash(p.email)})
    if not valid:
        raise Invalid("Your current password is incorrect.", "AUTH_CURRENT_PASSWORD_INCORRECT")
    if new_password == current_password:
        raise Invalid("Choose a password that differs from your current one.")
    with transaction(p.company_id, p.user_id) as conn:
        conn.execute(text("UPDATE core.users SET password_hash = :h WHERE id = :u"),
                     {"h": hash_password(new_password), "u": p.user_id})
        conn.execute(text("UPDATE core.sessions SET revoked_at = now()"
                          " WHERE user_id = :u AND revoked_at IS NULL AND token_hash <> :t"),
                     {"u": p.user_id, "t": _token_hash(session_token)})
        audit.record(conn, p.company_id, p.user_id, "auth.password_changed", "user", p.user_id)


def revoke_user_sessions(conn: Connection, user_id: UUID) -> None:
    conn.execute(
        text("UPDATE core.sessions SET revoked_at = now() WHERE user_id = :u AND revoked_at IS NULL"), {"u": user_id}
    )
