"""Audit events are written in the same transaction as the change they record."""

import json
from uuid import UUID

from sqlalchemy import Connection, text


def record(
    conn: Connection,
    company_id: UUID,
    actor_user_id: UUID | None,
    action: str,
    resource_type: str,
    resource_id: object | None = None,
    details: dict | None = None,
) -> None:
    conn.execute(
        text(
            "INSERT INTO core.audit_events (company_id, actor_user_id, action, resource_type, resource_id, details)"
            " VALUES (:c, :a, :action, :rt, :rid, CAST(:d AS jsonb))"
        ),
        {
            "c": company_id,
            "a": actor_user_id,
            "action": action,
            "rt": resource_type,
            "rid": str(resource_id) if resource_id is not None else None,
            "d": json.dumps(details or {}, default=str),
        },
    )
