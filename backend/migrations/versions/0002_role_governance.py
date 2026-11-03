"""Elevated-role governance and resettable model routes.

- roles.elevated marks roles that grant company-wide control (owner, security
  administrator). Approving them needs role.elevated.approve, held only by the
  company owner, in addition to the existing two-person rule.
- Department model-route overrides and module switches can be removed (configuration, not business
  data), so a department can fall back to the company default.

Revision ID: 0002
Revises: 0001
"""

import os

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

RUNTIME_ROLE = os.environ.get("ABTEILO_RUNTIME_ROLE", "abteilo_app")


def upgrade() -> None:
    op.execute("ALTER TABLE core.roles ADD COLUMN elevated boolean NOT NULL DEFAULT false")
    op.execute(
        "INSERT INTO core.permissions (code, description, risk) VALUES "
        "('role.elevated.approve', 'Approve grants of elevated company roles (owner, security administrator)', 'high')"
    )
    # Existing companies: the table owner is also subject to FORCE RLS, so update per company context.
    op.execute(
        """
        DO $$
        DECLARE c uuid;
        BEGIN
          FOR c IN SELECT DISTINCT company_id FROM core.memberships LOOP
            PERFORM set_config('app.company_id', c::text, true);
            UPDATE core.roles SET elevated = true WHERE code IN ('company_owner', 'security_admin');
            INSERT INTO core.role_permissions (company_id, role_code, permission_code)
              VALUES (c, 'company_owner', 'role.elevated.approve') ON CONFLICT DO NOTHING;
          END LOOP;
          PERFORM set_config('app.company_id', '', true);
        END $$;
        """
    )
    # Configuration rows (not business data): routes and module switches can be removed.
    op.execute(f"GRANT DELETE ON core.model_routes, core.department_modules TO {RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE DELETE ON core.model_routes, core.department_modules FROM {RUNTIME_ROLE}")
    op.execute(
        """
        DO $$
        DECLARE c uuid;
        BEGIN
          FOR c IN SELECT DISTINCT company_id FROM core.memberships LOOP
            PERFORM set_config('app.company_id', c::text, true);
            DELETE FROM core.role_permissions WHERE permission_code = 'role.elevated.approve';
          END LOOP;
          PERFORM set_config('app.company_id', '', true);
        END $$;
        """
    )
    op.execute("DELETE FROM core.permissions WHERE code = 'role.elevated.approve'")
    op.execute("ALTER TABLE core.roles DROP COLUMN elevated")
