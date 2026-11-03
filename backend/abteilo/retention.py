"""Chat retention (DATA-04), generated-report expiry and graph-read log retention (INT-09). Touches only conv.* rows, the user link of
old ai_runs and graph-read log rows older than 90 days (the database policy refuses anything younger);
business tables are out of reach by grant, not just by convention."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text

from abteilo import audit
from abteilo.db import transaction


@dataclass(frozen=True)
class PurgeResult:
    company_id: UUID
    retention_days: int | None
    conversations_deleted: int
    runs_pseudonymised: int
    graph_reads_deleted: int = 0
    reports_deleted: int = 0


def purge_company(company_id: UUID, actor_user_id: UUID | None = None) -> PurgeResult:
    with transaction(company_id, actor_user_id, retention_job=True) as conn:
        reads = conn.execute(text("DELETE FROM core.graph_read_log WHERE occurred_at < now() - interval '90 days'")).rowcount
        if reads:
            audit.record(conn, company_id, actor_user_id, "retention.graph_reads_purged", "company", company_id,
                         {"retention_days": 90, "rows_deleted": reads})
        # Generated reports expire on their own date (at most 30 days, shorter with chat retention).
        expired = conn.execute(text("DELETE FROM conv.reports WHERE expires_at <= now()")).rowcount
        if expired:
            audit.record(conn, company_id, actor_user_id, "retention.reports_purged", "company", company_id,
                         {"reports_deleted": expired})
        days = conn.execute(text("SELECT chat_retention_days FROM core.company_settings")).scalar()
        if days is None:
            return PurgeResult(company_id, None, 0, 0, reads, expired)
        deleted = conn.execute(
            text("DELETE FROM conv.conversations WHERE last_message_at < now() - make_interval(days => :d)"), {"d": days}
        ).rowcount
        pseudonymised = conn.execute(
            text("UPDATE core.ai_runs SET user_id = NULL"
                 " WHERE user_id IS NOT NULL AND created_at < now() - make_interval(days => :d)"),
            {"d": days},
        ).rowcount
        if deleted or pseudonymised:
            audit.record(conn, company_id, actor_user_id, "retention.chats_purged", "company", company_id,
                         {"retention_days": days, "conversations_deleted": deleted, "runs_pseudonymised": pseudonymised})
    return PurgeResult(company_id, days, deleted, pseudonymised, reads, expired)


def purge_all() -> list[PurgeResult]:
    with transaction() as conn:
        company_ids = conn.execute(text("SELECT DISTINCT company_id FROM core.memberships")).scalars().all()
    return [purge_company(cid) for cid in company_ids]
