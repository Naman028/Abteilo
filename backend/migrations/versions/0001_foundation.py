"""Foundation: identity, company structure, roles, department modules, documents,
AI registry, conversations, audit. Includes grants and Row-Level Security.

Revision ID: 0001
Revises:
"""

import os
import re

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

RUNTIME_ROLE = os.environ.get("ABTEILO_RUNTIME_ROLE", "abteilo_app")
if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", RUNTIME_ROLE):
    raise SystemExit("ABTEILO_RUNTIME_ROLE must be a plain lowercase identifier")

PERMISSIONS = [
    # company-wide administration
    ("company.settings.manage", "Change company settings, retention and external AI policy", "high"),
    ("department.manage", "Create, rename and archive departments", "high"),
    ("member.manage", "Add members and disable accounts", "high"),
    ("model.manage", "Manage AI model profiles and routing", "high"),
    ("ai.usage.read", "Read aggregated AI usage metrics", "low"),
    ("audit.read", "Read audit events", "medium"),
    ("report.executive.read", "Read aggregated cross-department reports", "medium"),
    # every employee
    ("ai.chat.use", "Use the assistant in departments the user belongs to", "low"),
    ("purchase.request.create", "Submit purchase requests", "low"),
    ("leave.request.create", "Submit own leave requests", "low"),
    # department scoped
    ("department.member", "Belong to a department workspace", "low"),
    ("documents.read", "Read department and company documents", "low"),
    ("documents.write", "Upload and archive department documents", "medium"),
    ("documents.confidential.read", "Read confidential department documents", "medium"),
    ("engineering.project.read", "Read engineering projects and tasks", "low"),
    ("engineering.task.write", "Create and update engineering tasks", "low"),
    ("engineering.project.manage", "Create projects and change project status", "medium"),
    ("finance.budget.read", "Read budgets and spending", "medium"),
    ("finance.budget.manage", "Set department budgets", "high"),
    ("finance.purchase.approve", "Approve or reject purchase requests", "high"),
    ("hr.leave.approve", "Approve or reject leave requests", "medium"),
    ("hr.employee.read", "Read employee leave balances", "medium"),
    ("it.access.propose", "Propose role changes", "medium"),
    ("it.access.approve", "Approve role changes", "high"),
]

# Tables carrying company_id that are protected by tenant RLS.
TENANT_TABLES = [
    "company_settings", "departments", "department_modules", "roles", "role_permissions",
    "role_assignments", "projects", "project_members", "tasks", "budgets", "purchase_requests",
    "leave_balances", "leave_requests", "access_requests", "documents", "document_versions",
    "document_chunks", "audit_events", "model_profiles", "model_routes", "ai_runs",
]

# Business and history tables: the runtime role may never DELETE these (DATA-01).
APPEND_ONLY = ["audit_events", "document_versions", "document_chunks"]

SCHEMA = r"""
CREATE SCHEMA core;
CREATE SCHEMA conv;

CREATE FUNCTION core.current_company_id() RETURNS uuid LANGUAGE sql STABLE AS
$$ SELECT NULLIF(current_setting('app.company_id', true), '')::uuid $$;
CREATE FUNCTION core.current_user_id() RETURNS uuid LANGUAGE sql STABLE AS
$$ SELECT NULLIF(current_setting('app.user_id', true), '')::uuid $$;
CREATE FUNCTION core.retention_job() RETURNS boolean LANGUAGE sql STABLE AS
$$ SELECT coalesce(current_setting('app.retention_job', true), '') = 'on' $$;

-- Identity plane (not tenant scoped; always filtered by trusted code) --------
CREATE TABLE core.users (
    id uuid PRIMARY KEY,
    email text NOT NULL CHECK (email = lower(email) AND length(email) BETWEEN 3 AND 254),
    display_name text NOT NULL CHECK (length(btrim(display_name)) BETWEEN 1 AND 120),
    password_hash text NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz,
    UNIQUE (email)
);

CREATE TABLE core.companies (
    id uuid PRIMARY KEY,
    name text NOT NULL CHECK (length(btrim(name)) BETWEEN 2 AND 160),
    slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
    industry text NOT NULL,
    timezone text NOT NULL DEFAULT 'Europe/Berlin',
    currency char(3) NOT NULL DEFAULT 'EUR',
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE core.memberships (
    company_id uuid NOT NULL REFERENCES core.companies(id) ON DELETE RESTRICT,
    user_id uuid NOT NULL REFERENCES core.users(id) ON DELETE RESTRICT,
    title text,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    joined_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, user_id)
);

CREATE TABLE core.sessions (
    token_hash bytea PRIMARY KEY CHECK (length(token_hash) = 32),
    user_id uuid NOT NULL REFERENCES core.users(id),
    company_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    FOREIGN KEY (company_id, user_id) REFERENCES core.memberships(company_id, user_id),
    CHECK (expires_at > created_at)
);
CREATE INDEX sessions_user_idx ON core.sessions (user_id);

-- Stores a hash of the normalised email only (data minimisation).
CREATE TABLE core.login_failures (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email_hash bytea NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX login_failures_lookup_idx ON core.login_failures (email_hash, occurred_at);

CREATE TABLE core.permissions (
    code text PRIMARY KEY,
    description text NOT NULL,
    risk text NOT NULL CHECK (risk IN ('low', 'medium', 'high'))
);

-- Company structure -------------------------------------------------------------
CREATE TABLE core.company_settings (
    company_id uuid PRIMARY KEY REFERENCES core.companies(id),
    chat_retention_days integer CHECK (chat_retention_days IN (30, 60)),
    external_ai_enabled boolean NOT NULL DEFAULT false,
    external_ai_confirmed_by uuid REFERENCES core.users(id),
    external_ai_confirmed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (NOT external_ai_enabled OR (external_ai_confirmed_by IS NOT NULL AND external_ai_confirmed_at IS NOT NULL))
);

CREATE TABLE core.departments (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL REFERENCES core.companies(id),
    code text NOT NULL CHECK (code ~ '^[a-z][a-z0-9_]{1,40}$'),
    name text NOT NULL CHECK (length(btrim(name)) BETWEEN 2 AND 80),
    template text NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (company_id, code),
    UNIQUE (company_id, id)
);

CREATE TABLE core.department_modules (
    company_id uuid NOT NULL,
    department_id uuid NOT NULL,
    module_code text NOT NULL,
    PRIMARY KEY (company_id, department_id, module_code),
    FOREIGN KEY (company_id, department_id) REFERENCES core.departments(company_id, id)
);

CREATE TABLE core.roles (
    company_id uuid NOT NULL REFERENCES core.companies(id),
    code text NOT NULL CHECK (code ~ '^[a-z][a-z0-9_]{1,60}$'),
    name text NOT NULL,
    scope text NOT NULL CHECK (scope IN ('company', 'department')),
    system_managed boolean NOT NULL DEFAULT true,
    PRIMARY KEY (company_id, code),
    UNIQUE (company_id, code, scope)
);

CREATE TABLE core.role_permissions (
    company_id uuid NOT NULL,
    role_code text NOT NULL,
    permission_code text NOT NULL REFERENCES core.permissions(code),
    PRIMARY KEY (company_id, role_code, permission_code),
    FOREIGN KEY (company_id, role_code) REFERENCES core.roles(company_id, code)
);

-- The generated scope column plus the composite FK make it impossible to
-- attach a company role to a department or a department role to the company.
CREATE TABLE core.role_assignments (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL,
    user_id uuid NOT NULL,
    role_code text NOT NULL,
    department_id uuid,
    role_scope text GENERATED ALWAYS AS (CASE WHEN department_id IS NULL THEN 'company' ELSE 'department' END) STORED,
    granted_by uuid REFERENCES core.users(id),
    granted_at timestamptz NOT NULL DEFAULT now(),
    revoked_by uuid REFERENCES core.users(id),
    revoked_at timestamptz,
    FOREIGN KEY (company_id, user_id) REFERENCES core.memberships(company_id, user_id),
    FOREIGN KEY (company_id, role_code, role_scope) REFERENCES core.roles(company_id, code, scope),
    FOREIGN KEY (company_id, department_id) REFERENCES core.departments(company_id, id),
    CHECK ((revoked_at IS NULL) = (revoked_by IS NULL))
);
CREATE UNIQUE INDEX role_assignments_active_uq ON core.role_assignments
    (company_id, user_id, role_code, coalesce(department_id, '00000000-0000-0000-0000-000000000000'::uuid))
    WHERE revoked_at IS NULL;

-- Engineering -------------------------------------------------------------------
CREATE TABLE core.projects (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL,
    department_id uuid NOT NULL,
    code text NOT NULL CHECK (code ~ '^[A-Z][A-Z0-9-]{1,19}$'),
    name text NOT NULL CHECK (length(btrim(name)) BETWEEN 2 AND 120),
    description text NOT NULL DEFAULT '' CHECK (length(description) <= 4000),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('planned', 'active', 'blocked', 'done', 'archived')),
    created_by uuid NOT NULL REFERENCES core.users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (company_id, code),
    UNIQUE (company_id, id),
    FOREIGN KEY (company_id, department_id) REFERENCES core.departments(company_id, id)
);

CREATE TABLE core.project_members (
    company_id uuid NOT NULL,
    project_id uuid NOT NULL,
    user_id uuid NOT NULL,
    PRIMARY KEY (company_id, project_id, user_id),
    FOREIGN KEY (company_id, project_id) REFERENCES core.projects(company_id, id),
    FOREIGN KEY (company_id, user_id) REFERENCES core.memberships(company_id, user_id)
);

CREATE TABLE core.tasks (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL,
    project_id uuid NOT NULL,
    title text NOT NULL CHECK (length(btrim(title)) BETWEEN 2 AND 200),
    status text NOT NULL DEFAULT 'todo' CHECK (status IN ('todo', 'in_progress', 'blocked', 'done')),
    assignee_id uuid,
    due_date date,
    created_by uuid NOT NULL REFERENCES core.users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (company_id, project_id) REFERENCES core.projects(company_id, id),
    FOREIGN KEY (company_id, assignee_id) REFERENCES core.memberships(company_id, user_id)
);
CREATE INDEX tasks_project_idx ON core.tasks (company_id, project_id);

-- Finance -----------------------------------------------------------------------
CREATE TABLE core.budgets (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL,
    department_id uuid NOT NULL,
    period text NOT NULL CHECK (period ~ '^[0-9]{4}-Q[1-4]$'),
    amount_cents bigint NOT NULL CHECK (amount_cents >= 0),
    UNIQUE (company_id, department_id, period),
    FOREIGN KEY (company_id, department_id) REFERENCES core.departments(company_id, id)
);

CREATE TABLE core.purchase_requests (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL,
    department_id uuid NOT NULL,
    project_id uuid,
    title text NOT NULL CHECK (length(btrim(title)) BETWEEN 2 AND 200),
    justification text NOT NULL CHECK (length(justification) BETWEEN 1 AND 4000),
    amount_cents bigint NOT NULL CHECK (amount_cents > 0 AND amount_cents <= 100000000000),
    period text NOT NULL CHECK (period ~ '^[0-9]{4}-Q[1-4]$'),
    status text NOT NULL DEFAULT 'submitted' CHECK (status IN ('submitted', 'approved', 'rejected', 'cancelled')),
    requested_by uuid NOT NULL,
    decided_by uuid,
    decided_at timestamptz,
    decision_note text CHECK (length(decision_note) <= 2000),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (company_id, department_id) REFERENCES core.departments(company_id, id),
    FOREIGN KEY (company_id, project_id) REFERENCES core.projects(company_id, id),
    FOREIGN KEY (company_id, requested_by) REFERENCES core.memberships(company_id, user_id),
    FOREIGN KEY (company_id, decided_by) REFERENCES core.memberships(company_id, user_id),
    CHECK (decided_by IS NULL OR decided_by <> requested_by),
    CHECK ((status IN ('approved', 'rejected')) = (decided_by IS NOT NULL AND decided_at IS NOT NULL))
);

-- Human resources ---------------------------------------------------------------
CREATE TABLE core.leave_balances (
    company_id uuid NOT NULL,
    user_id uuid NOT NULL,
    year integer NOT NULL CHECK (year BETWEEN 2000 AND 2100),
    days_total numeric(5,1) NOT NULL CHECK (days_total BETWEEN 0 AND 100),
    PRIMARY KEY (company_id, user_id, year),
    FOREIGN KEY (company_id, user_id) REFERENCES core.memberships(company_id, user_id)
);

CREATE TABLE core.leave_requests (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL,
    user_id uuid NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL,
    days numeric(5,1) NOT NULL CHECK (days > 0 AND days <= 100),
    status text NOT NULL DEFAULT 'submitted' CHECK (status IN ('submitted', 'approved', 'rejected', 'cancelled')),
    decided_by uuid,
    decided_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (company_id, user_id) REFERENCES core.memberships(company_id, user_id),
    FOREIGN KEY (company_id, decided_by) REFERENCES core.memberships(company_id, user_id),
    CHECK (end_date >= start_date),
    CHECK (decided_by IS NULL OR decided_by <> user_id),
    CHECK ((status IN ('approved', 'rejected')) = (decided_by IS NOT NULL AND decided_at IS NOT NULL))
);

-- IT & Security -----------------------------------------------------------------
CREATE TABLE core.access_requests (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL,
    target_user_id uuid NOT NULL,
    role_code text NOT NULL,
    department_id uuid,
    role_scope text GENERATED ALWAYS AS (CASE WHEN department_id IS NULL THEN 'company' ELSE 'department' END) STORED,
    justification text NOT NULL CHECK (length(justification) BETWEEN 1 AND 2000),
    status text NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'approved', 'rejected')),
    proposed_by uuid NOT NULL,
    decided_by uuid,
    decided_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (company_id, target_user_id) REFERENCES core.memberships(company_id, user_id),
    FOREIGN KEY (company_id, proposed_by) REFERENCES core.memberships(company_id, user_id),
    FOREIGN KEY (company_id, decided_by) REFERENCES core.memberships(company_id, user_id),
    FOREIGN KEY (company_id, role_code, role_scope) REFERENCES core.roles(company_id, code, scope),
    FOREIGN KEY (company_id, department_id) REFERENCES core.departments(company_id, id),
    CHECK (decided_by IS NULL OR (decided_by <> proposed_by AND decided_by <> target_user_id)),
    CHECK ((status IN ('approved', 'rejected')) = (decided_by IS NOT NULL AND decided_at IS NOT NULL))
);

-- Documents ---------------------------------------------------------------------
CREATE TABLE core.documents (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL,
    department_id uuid NOT NULL,
    title text NOT NULL CHECK (length(btrim(title)) BETWEEN 2 AND 200),
    classification text NOT NULL CHECK (classification IN ('company', 'department', 'confidential')),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    current_version integer NOT NULL DEFAULT 1 CHECK (current_version >= 1),
    created_by uuid NOT NULL REFERENCES core.users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (company_id, id),
    FOREIGN KEY (company_id, department_id) REFERENCES core.departments(company_id, id)
);

CREATE TABLE core.document_versions (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL,
    document_id uuid NOT NULL,
    version integer NOT NULL CHECK (version >= 1),
    content text NOT NULL,
    content_sha256 char(64) NOT NULL,
    size_bytes integer NOT NULL CHECK (size_bytes BETWEEN 1 AND 1048576),
    created_by uuid NOT NULL REFERENCES core.users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, version),
    UNIQUE (company_id, id),
    FOREIGN KEY (company_id, document_id) REFERENCES core.documents(company_id, id)
);

CREATE TABLE core.document_chunks (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL,
    document_id uuid NOT NULL,
    version_id uuid NOT NULL,
    chunk_index integer NOT NULL CHECK (chunk_index >= 0),
    content text NOT NULL CHECK (length(content) BETWEEN 1 AND 4000),
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
    UNIQUE (version_id, chunk_index),
    FOREIGN KEY (company_id, document_id) REFERENCES core.documents(company_id, id),
    FOREIGN KEY (company_id, version_id) REFERENCES core.document_versions(company_id, id)
);
CREATE INDEX document_chunks_tsv_idx ON core.document_chunks USING gin (tsv);

-- Audit -------------------------------------------------------------------------
CREATE TABLE core.audit_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id uuid NOT NULL REFERENCES core.companies(id),
    actor_user_id uuid REFERENCES core.users(id),
    action text NOT NULL CHECK (action ~ '^[a-z_]+\.[a-z_.]+$'),
    resource_type text NOT NULL,
    resource_id text,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_events_company_time_idx ON core.audit_events (company_id, occurred_at DESC);

-- AI registry and usage ---------------------------------------------------------
CREATE TABLE core.model_profiles (
    company_id uuid NOT NULL REFERENCES core.companies(id),
    code text NOT NULL CHECK (code ~ '^[a-z][a-z0-9_]{1,40}$'),
    display_name text NOT NULL CHECK (length(btrim(display_name)) BETWEEN 2 AND 80),
    provider text NOT NULL CHECK (provider IN ('ollama', 'openai_compatible', 'anthropic', 'scripted')),
    model_name text NOT NULL CHECK (model_name ~ '^[A-Za-z0-9._:/-]{1,100}$'),
    purpose text NOT NULL DEFAULT '' CHECK (length(purpose) <= 300),
    max_output_tokens integer NOT NULL DEFAULT 1500 CHECK (max_output_tokens BETWEEN 64 AND 16000),
    temperature numeric(3,2) NOT NULL DEFAULT 0.2 CHECK (temperature BETWEEN 0 AND 1),
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, code)
);

CREATE TABLE core.model_routes (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL,
    department_id uuid,
    task_class text NOT NULL CHECK (task_class IN ('general', 'heavy')),
    profile_code text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (company_id, profile_code) REFERENCES core.model_profiles(company_id, code),
    FOREIGN KEY (company_id, department_id) REFERENCES core.departments(company_id, id)
);
CREATE UNIQUE INDEX model_routes_uq ON core.model_routes
    (company_id, coalesce(department_id, '00000000-0000-0000-0000-000000000000'::uuid), task_class);

-- Usage metadata only: never message content (AI-05).
CREATE TABLE core.ai_runs (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL REFERENCES core.companies(id),
    user_id uuid REFERENCES core.users(id),
    department_id uuid,
    conversation_id uuid,
    profile_code text NOT NULL,
    provider text NOT NULL,
    model_name text NOT NULL,
    task_class text NOT NULL,
    stage text NOT NULL CHECK (stage IN ('plan', 'answer')),
    status text NOT NULL CHECK (status IN ('ok', 'error', 'invalid_output')),
    error_code text,
    input_tokens integer NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens integer NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    latency_ms integer NOT NULL CHECK (latency_ms >= 0),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ai_runs_company_time_idx ON core.ai_runs (company_id, created_at DESC);

-- Conversations (separate schema, owner-private, deletable) -----------------------
CREATE TABLE conv.conversations (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL REFERENCES core.companies(id),
    user_id uuid NOT NULL REFERENCES core.users(id),
    department_id uuid NOT NULL,
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
    created_at timestamptz NOT NULL DEFAULT now(),
    last_message_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX conversations_owner_idx ON conv.conversations (company_id, user_id, last_message_at DESC);
CREATE INDEX conversations_retention_idx ON conv.conversations (company_id, last_message_at);

CREATE TABLE conv.messages (
    id uuid PRIMARY KEY,
    company_id uuid NOT NULL,
    conversation_id uuid NOT NULL REFERENCES conv.conversations(id) ON DELETE CASCADE,
    user_id uuid NOT NULL,
    role text NOT NULL CHECK (role IN ('user', 'assistant')),
    content text NOT NULL CHECK (length(content) <= 20000),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX messages_conversation_idx ON conv.messages (conversation_id, created_at);
"""


def upgrade() -> None:
    op.execute(SCHEMA)
    for code, description, risk in PERMISSIONS:
        op.execute(
            f"INSERT INTO core.permissions (code, description, risk) VALUES "
            f"('{code}', '{description}', '{risk}')"
        )

    # Row-Level Security: tenant scope for every company table.
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE core.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE core.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_scope ON core.{table} "
            f"USING (company_id = core.current_company_id()) "
            f"WITH CHECK (company_id = core.current_company_id())"
        )
    op.execute("ALTER TABLE core.companies ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE core.companies FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_scope ON core.companies "
        "USING (id = core.current_company_id()) WITH CHECK (id = core.current_company_id())"
    )
    # Conversations are private to their owner; only the retention job sees all.
    for table in ("conversations", "messages"):
        op.execute(f"ALTER TABLE conv.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE conv.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY owner_scope ON conv.{table} "
            f"USING (company_id = core.current_company_id() "
            f"AND (user_id = core.current_user_id() OR core.retention_job())) "
            f"WITH CHECK (company_id = core.current_company_id() AND user_id = core.current_user_id())"
        )

    # Grants: no DELETE on business data, append-only history, private chats.
    role = RUNTIME_ROLE
    op.execute(f"GRANT USAGE ON SCHEMA core, conv TO {role}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA core TO {role}")
    for table in APPEND_ONLY:
        op.execute(f"REVOKE UPDATE ON core.{table} FROM {role}")
    op.execute(f"REVOKE INSERT, UPDATE ON core.permissions FROM {role}")
    op.execute(f"REVOKE UPDATE ON core.ai_runs FROM {role}")
    op.execute(f"GRANT UPDATE (user_id) ON core.ai_runs TO {role}")
    op.execute(f"GRANT DELETE ON core.sessions, core.login_failures TO {role}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA conv TO {role}")
    op.execute(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA core TO {role}")
    op.execute(f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA core TO {role}")


def downgrade() -> None:
    op.execute("DROP SCHEMA conv CASCADE")
    op.execute("DROP SCHEMA core CASCADE")
