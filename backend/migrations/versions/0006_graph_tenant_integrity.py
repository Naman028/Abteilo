"""Tenant-safe graph person references (GRAPH-11).

Defect: the person projection read core.memberships, which has no RLS, without a
company filter, so every company's graph received every company's people.

This migration makes the database refuse that state instead of trusting the
projection query:

- person_user_id is derived from the row itself (ref_id for a non-quarantined
  person, NULL otherwise) and references core.memberships(company_id, user_id).
  The composite key means a person entity of company A must point at a
  membership in company A; a membership only in company B does not satisfy it.
  Non-person entities have NULL there and are not touched by the key.
- Rows that already violate it are quarantined, not deleted: deactivated,
  label replaced (it was another company's personal data), reason and time
  recorded, one audit event per affected company with the count. Their current
  edges are ended and open suggestions on them superseded. A quarantined row can
  never be active again, and the runtime role can neither set nor clear the
  quarantine. The key is added NOT VALID, the rows are remediated, then the key
  is validated over the whole table.
- Uniqueness of (company_id, kind, ref_id) now covers non-quarantined rows only,
  so a person who later legitimately joins the company gets a fresh, validated
  entity instead of reviving the quarantined one.

Downgrade is refused while quarantined rows exist: it would erase the record of
what was quarantined.

Revision ID: 0006
Revises: 0005
"""

import os

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

RUNTIME_ROLE = os.environ.get("ABTEILO_RUNTIME_ROLE", "abteilo_app")

INSERTABLE = "id, company_id, kind, ref_id, label, status, department_id, vis_scope, vis_department_id, vis_user_id, active, projected_at"

# Runs once per company: core.graph_entities has FORCE ROW LEVEL SECURITY, which binds the owner too.
QUARANTINE = """
WITH foreign_people AS (
    UPDATE core.graph_entities e
    SET active = false, label = 'Quarantined person', quarantine_reason = 'foreign_membership', quarantined_at = now()
    WHERE e.kind = 'person' AND e.quarantine_reason IS NULL
      AND NOT EXISTS (SELECT 1 FROM core.memberships m WHERE m.company_id = e.company_id AND m.user_id = e.ref_id)
    RETURNING e.id
), ended AS (
    UPDATE core.graph_relationships r SET valid_to = now(), ended_reason = 'source_archived'
    WHERE r.valid_to IS NULL AND (r.src_id IN (SELECT id FROM foreign_people) OR r.dst_id IN (SELECT id FROM foreign_people))
    RETURNING r.id
), superseded AS (
    UPDATE core.graph_candidates c SET status = 'superseded'
    WHERE c.status = 'proposed' AND (c.src_id IN (SELECT id FROM foreign_people) OR c.dst_id IN (SELECT id FROM foreign_people))
    RETURNING c.id
)
INSERT INTO core.audit_events (company_id, action, resource_type, details)
SELECT core.current_company_id(), 'graph.entities_quarantined', 'graph_entity',
       jsonb_build_object('migration', '0006', 'reason', 'foreign_membership', 'entities', count(*),
                          'relationships_ended', (SELECT count(*) FROM ended),
                          'candidates_superseded', (SELECT count(*) FROM superseded))
FROM foreign_people HAVING count(*) > 0
"""


def upgrade() -> None:
    op.execute("""
        ALTER TABLE core.graph_entities
            ADD COLUMN quarantine_reason text CHECK (quarantine_reason IN ('foreign_membership')),
            ADD COLUMN quarantined_at timestamptz,
            ADD CONSTRAINT graph_entities_quarantine_recorded CHECK ((quarantine_reason IS NULL) = (quarantined_at IS NULL))""")
    op.execute("""
        ALTER TABLE core.graph_entities
            ADD COLUMN person_user_id uuid
                GENERATED ALWAYS AS (CASE WHEN kind = 'person' AND quarantine_reason IS NULL THEN ref_id END) STORED""")
    # Blocks new violations immediately; existing rows are remediated below, then validated.
    op.execute("""
        ALTER TABLE core.graph_entities ADD CONSTRAINT graph_entities_person_membership_fk
            FOREIGN KEY (company_id, person_user_id) REFERENCES core.memberships(company_id, user_id) NOT VALID""")

    companies = op.get_bind().exec_driver_sql("SELECT DISTINCT company_id FROM core.memberships").scalars().all()
    for company_id in companies:
        op.execute(f"SELECT set_config('app.company_id', '{company_id}', true)")
        op.execute(QUARANTINE)
    op.execute("SELECT set_config('app.company_id', '', true)")

    op.execute("ALTER TABLE core.graph_entities VALIDATE CONSTRAINT graph_entities_person_membership_fk")
    op.execute("""
        ALTER TABLE core.graph_entities
            ADD CONSTRAINT graph_entities_quarantine_inactive
                CHECK (quarantine_reason IS NULL OR (kind = 'person' AND NOT active)),
            ADD CONSTRAINT graph_entities_active_person_member
                CHECK (NOT active OR kind <> 'person' OR person_user_id IS NOT NULL),
            DROP CONSTRAINT graph_entities_company_id_kind_ref_id_key""")
    op.execute("CREATE UNIQUE INDEX graph_entities_current_ref_uq ON core.graph_entities (company_id, kind, ref_id)"
               " WHERE quarantine_reason IS NULL")
    # The runtime role writes only projection columns; it can neither quarantine nor release a row.
    op.execute(f"REVOKE INSERT ON core.graph_entities FROM {RUNTIME_ROLE}")
    op.execute(f"GRANT INSERT ({INSERTABLE}) ON core.graph_entities TO {RUNTIME_ROLE}")


def downgrade() -> None:
    # Validation scans every row regardless of RLS: fails while any quarantined row exists.
    op.execute("ALTER TABLE core.graph_entities ADD CONSTRAINT graph_entities_downgrade_requires_no_quarantine"
               " CHECK (quarantine_reason IS NULL)")
    op.execute(f"REVOKE INSERT ON core.graph_entities FROM {RUNTIME_ROLE}")
    op.execute(f"GRANT INSERT ON core.graph_entities TO {RUNTIME_ROLE}")
    op.execute("DROP INDEX core.graph_entities_current_ref_uq")
    op.execute("""
        ALTER TABLE core.graph_entities
            DROP CONSTRAINT graph_entities_downgrade_requires_no_quarantine,
            DROP CONSTRAINT graph_entities_active_person_member,
            DROP CONSTRAINT graph_entities_quarantine_inactive,
            DROP CONSTRAINT graph_entities_person_membership_fk,
            DROP COLUMN person_user_id,
            DROP COLUMN quarantined_at,
            DROP COLUMN quarantine_reason,
            ADD CONSTRAINT graph_entities_company_id_kind_ref_id_key UNIQUE (company_id, kind, ref_id)""")
