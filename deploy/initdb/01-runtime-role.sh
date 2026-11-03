#!/bin/sh
# Creates the restricted runtime role on first database start. The application
# connects as this role: it owns nothing, cannot bypass RLS and receives only
# the grants issued by the migrations.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v app_password="$ABTEILO_APP_PASSWORD" <<'SQL'
CREATE ROLE abteilo_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD :'app_password';
REVOKE ALL ON SCHEMA public FROM PUBLIC;
SQL
