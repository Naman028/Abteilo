"""Operator password reset, for when nobody can reset a password in the console (for example a forgotten
owner password). Requires access to the server configuration and database; the API deliberately has no
unauthenticated reset endpoint.

    python -m abteilo.reset_password owner@bina.example   # prompts twice for the new password
    python -m abteilo.reset_password --demo               # every demo account gets ABTEILO_DEMO_PASSWORD from .env

The password is never a command-line argument (it would land in shell history). Each reset signs the person
out everywhere, clears their login throttling and records an audit event in each of their companies.
"""

import argparse
import getpass
import sys

from sqlalchemy import text

from abteilo import audit
from abteilo.config import get_settings
from abteilo.db import transaction
from abteilo.security import MIN_PASSWORD_LENGTH, _active_company, _email_hash, hash_password, revoke_user_sessions
from abteilo.seed import SLUG


def reset(email: str, password: str) -> bool:
    """Set a new password for one account. Returns False when no such account exists."""
    email = email.strip().lower()
    password_hash = hash_password(password)
    with transaction() as conn:
        user = conn.execute(text("SELECT id FROM core.users WHERE email = :e"), {"e": email}).first()
        if user is None:
            return False
        companies = conn.execute(
            text("SELECT company_id FROM core.memberships WHERE user_id = :u"), {"u": user.id}).scalars().all()
    for company_id in companies or [None]:
        with transaction(company_id) as conn:
            conn.execute(text("UPDATE core.users SET password_hash = :h WHERE id = :u"), {"h": password_hash, "u": user.id})
            revoke_user_sessions(conn, user.id)
            conn.execute(text("DELETE FROM core.login_failures WHERE email_hash = :h"), {"h": _email_hash(email)})
            if company_id is not None:
                audit.record(conn, company_id, None, "member.password_reset", "user", user.id, {"via": "operator_cli"})
    return True


def demo_accounts() -> list[str]:
    """All accounts of the seeded demo company."""
    with transaction() as conn:
        owner = conn.execute(text("SELECT id FROM core.users WHERE email = :e"), {"e": f"owner@{SLUG}.example"}).first()
        company_id = _active_company(conn, owner.id, SLUG) if owner else None
    if company_id is None:
        return []
    with transaction(company_id) as conn:
        return conn.execute(text("SELECT u.email FROM core.memberships m JOIN core.users u ON u.id = m.user_id"
                                 " WHERE m.company_id = :c ORDER BY u.email"), {"c": company_id}).scalars().all()


def _prompt() -> str:
    password = getpass.getpass(f"New password (at least {MIN_PASSWORD_LENGTH} characters): ")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"Too short: use at least {MIN_PASSWORD_LENGTH} characters.")
    if getpass.getpass("Repeat it: ") != password:
        sys.exit("The two entries differ; nothing was changed.")
    return password


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m abteilo.reset_password", description=__doc__.split("\n\n")[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("email", nargs="?", help="account to reset")
    target.add_argument("--demo", action="store_true", help="reset every demo account to ABTEILO_DEMO_PASSWORD")
    args = parser.parse_args()

    if args.demo:
        emails = demo_accounts()
        if not emails:
            sys.exit("No demo company found. Create it with: python -m abteilo.seed")
        secret = get_settings().demo_password
        password = secret.get_secret_value() if secret else ""
        if len(password) < MIN_PASSWORD_LENGTH:
            sys.exit(f"Set ABTEILO_DEMO_PASSWORD in .env (at least {MIN_PASSWORD_LENGTH} characters) first.")
        for email in emails:
            reset(email, password)
        print(f"Reset {len(emails)} demo accounts to ABTEILO_DEMO_PASSWORD from .env:")
        print("\n".join(f"  {e}" for e in emails))
        return

    if not reset(args.email, _prompt()):
        sys.exit("No account with that email; nothing was changed.")
    print(f"Password reset for {args.email.strip().lower()}. Their existing sessions were signed out.")


if __name__ == "__main__":
    main()
