"""Alembic runs as the schema-owner role, never as the runtime role."""

from alembic import context
from sqlalchemy import create_engine

from abteilo.config import get_settings

url = context.config.attributes.get("url")
if url is None:
    owner_url = get_settings().migration_database_url
    if owner_url is None:
        raise SystemExit("ABTEILO_MIGRATION_DATABASE_URL is required to run migrations")
    url = owner_url.get_secret_value()

engine = create_engine(url)
with engine.begin() as connection:
    context.configure(connection=connection, transactional_ddl=True)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
