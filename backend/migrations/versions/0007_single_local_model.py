"""One local generative model by default: drop the seeded Engineering heavy override.

Companies seeded before this revision route Engineering `heavy` work to
`engineering_local` (qwen2.5-coder:7b), a second generative model. The new
default is one model for the whole company (the company default route,
qwen2.5:7b).

Only the untouched seeded override is removed: a route an administrator set
through the console has a `model.route_set` audit event for that department and
task class, and is kept as it is. Each removal writes a `model.route_reset`
audit event (no actor; details name this migration). Model profiles are not
changed, so the override can be set again at any time.

Revision ID: 0007
Revises: 0006
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

# Runs once per company: model_routes and audit_events have FORCE ROW LEVEL SECURITY, which binds the owner too.
RESET = """
WITH seeded AS (
    SELECT r.id, r.department_id FROM core.model_routes r JOIN core.departments d ON d.id = r.department_id
    WHERE d.template = 'engineering' AND r.task_class = 'heavy' AND r.profile_code = 'engineering_local'
      AND NOT EXISTS (SELECT 1 FROM core.audit_events a WHERE a.action = 'model.route_set'
                      AND a.details->>'department_id' = r.department_id::text AND a.resource_id = 'heavy')
), removed AS (
    DELETE FROM core.model_routes r USING seeded s WHERE r.id = s.id RETURNING s.department_id
)
INSERT INTO core.audit_events (company_id, action, resource_type, resource_id, details)
SELECT core.current_company_id(), 'model.route_reset', 'model_route', 'heavy',
       jsonb_build_object('department_id', department_id::text, 'migration', '0007',
                          'reason', 'single_local_model_default', 'previous_profile', 'engineering_local')
FROM removed
"""

# Downgrade restores the seeded override where an Engineering department has no heavy route of its own.
RESTORE = """
INSERT INTO core.model_routes (id, company_id, department_id, task_class, profile_code)
SELECT gen_random_uuid(), d.company_id, d.id, 'heavy', 'engineering_local'
FROM core.departments d
WHERE d.template = 'engineering'
  AND EXISTS (SELECT 1 FROM core.model_profiles p WHERE p.code = 'engineering_local')
  AND NOT EXISTS (SELECT 1 FROM core.model_routes r WHERE r.department_id = d.id AND r.task_class = 'heavy')
"""


def _each_company(sql: str) -> None:
    companies = op.get_bind().exec_driver_sql("SELECT DISTINCT company_id FROM core.memberships").scalars().all()
    for company_id in companies:
        op.execute(f"SELECT set_config('app.company_id', '{company_id}', true)")
        op.execute(sql)
    op.execute("SELECT set_config('app.company_id', '', true)")


def upgrade() -> None:
    _each_company(RESET)


def downgrade() -> None:
    _each_company(RESTORE)
