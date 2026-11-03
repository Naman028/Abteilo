"""Versioned, server-owned templates for departments, roles and default models.

Departments are data: the owner can add any number of departments from these
templates. Roles are defined once per company; department-scoped roles are
reused by every department, so adding a department needs no new roles.
"""

from dataclasses import dataclass

TEMPLATE_VERSION = 1


@dataclass(frozen=True)
class DepartmentTemplate:
    code: str
    name: str
    modules: tuple[str, ...]
    tools: tuple[str, ...]
    assistant_focus: str


_COMMON_TOOLS = ("documents.search", "graph.related")

DEPARTMENT_TEMPLATES: dict[str, DepartmentTemplate] = {
    t.code: t
    for t in (
        DepartmentTemplate(
            "engineering", "Engineering", ("projects", "documents", "assistant"),
            ("engineering.list_projects", "engineering.get_project", *_COMMON_TOOLS),
            "engineering projects, tasks, blockers and technical documentation",
        ),
        DepartmentTemplate(
            "finance", "Finance", ("finance", "documents", "assistant"),
            ("finance.budget_summary", "finance.list_purchase_requests", *_COMMON_TOOLS),
            "budgets, spending and purchase approvals",
        ),
        DepartmentTemplate(
            "human_resources", "Human Resources", ("hr", "documents", "assistant"),
            ("hr.my_leave", "hr.pending_leave", *_COMMON_TOOLS),
            "HR policies, onboarding and leave",
        ),
        DepartmentTemplate(
            "it_security", "IT & Security", ("it", "documents", "assistant"),
            ("it.list_access_requests", "it.role_catalog", *_COMMON_TOOLS),
            "accounts, roles, access requests and IT policies",
        ),
        DepartmentTemplate(
            "custom", "Custom department", ("documents", "assistant"),
            _COMMON_TOOLS,
            "this department's documents and procedures",
        ),
    )
}

# The owner workspace is not a department; its assistant only gets aggregates.
OWNER_TOOLS = ("company.overview", "company.department_activity", "graph.related")

MAJOR_DEPARTMENTS = ("engineering", "finance", "human_resources", "it_security")


@dataclass(frozen=True)
class RoleTemplate:
    code: str
    name: str
    scope: str
    permissions: tuple[str, ...]
    elevated: bool = False  # grants company-wide control; needs owner approval to assign


_MEMBER = ("department.member", "documents.read")

ROLE_TEMPLATES: tuple[RoleTemplate, ...] = (
    RoleTemplate(
        "company_owner", "Company owner", "company",
        ("company.settings.manage", "department.manage", "member.manage", "model.manage",
         "ai.usage.read", "audit.read", "report.executive.read", "role.elevated.approve"),
        elevated=True,
    ),
    RoleTemplate("security_admin", "Security administrator", "company", ("it.access.approve", "audit.read"),
                 elevated=True),
    RoleTemplate("employee", "Employee", "company", ("ai.chat.use", "purchase.request.create", "leave.request.create")),
    RoleTemplate("department_member", "Department member", "department", _MEMBER),
    RoleTemplate("department_editor", "Department editor", "department", (*_MEMBER, "documents.write")),
    RoleTemplate(
        "department_lead", "Department lead", "department",
        (*_MEMBER, "documents.write", "documents.confidential.read"),
    ),
    RoleTemplate(
        "engineering_contributor", "Engineering contributor", "department",
        (*_MEMBER, "engineering.project.read", "engineering.task.write"),
    ),
    RoleTemplate(
        "engineering_lead", "Engineering lead", "department",
        (*_MEMBER, "documents.write", "documents.confidential.read", "engineering.project.read",
         "engineering.task.write", "engineering.project.manage"),
    ),
    RoleTemplate(
        "finance_analyst", "Finance analyst", "department",
        (*_MEMBER, "documents.write", "documents.confidential.read", "finance.budget.read", "finance.budget.manage",
         "finance.purchase.approve"),
    ),
    RoleTemplate(
        "hr_specialist", "HR specialist", "department",
        (*_MEMBER, "documents.write", "documents.confidential.read", "hr.leave.approve", "hr.employee.read"),
    ),
    RoleTemplate(
        "it_access_admin", "IT access administrator", "department",
        (*_MEMBER, "documents.write", "it.access.propose"),
    ),
)


@dataclass(frozen=True)
class ModelProfileTemplate:
    code: str
    display_name: str
    provider: str
    model_name: str
    purpose: str
    max_output_tokens: int
    enabled: bool


# Free, open-source models served locally by Ollama are the default. Hosted
# providers are present but disabled until the owner enables external AI.
MODEL_TEMPLATES: tuple[ModelProfileTemplate, ...] = (
    ModelProfileTemplate("general_local", "Qwen 2.5 7B (local)", "ollama", "qwen2.5:7b",
                         "Default model for everyday department work", 1500, True),
    ModelProfileTemplate("light_local", "Llama 3.2 3B (local)", "ollama", "llama3.2",
                         "Fast lightweight model for short answers", 1000, True),
    ModelProfileTemplate("engineering_local", "Qwen 2.5 Coder 7B (local)", "ollama", "qwen2.5-coder:7b",
                         "Dedicated engineering model for code and technical analysis", 2000, True),
    ModelProfileTemplate("heavy_claude", "Claude Opus 5.5 (hosted)", "anthropic", "claude-opus-5-5",
                         "Heavy reasoning; requires external AI to be enabled", 4000, False),
)

# (department code or None for company default, task class, profile code)
# One local generative model for the whole company by default: every department and the owner use
# qwen2.5:7b for general and heavy work. Other profiles stay available for explicit department overrides.
DEFAULT_ROUTES: tuple[tuple[str | None, str, str], ...] = (
    (None, "general", "general_local"),
    (None, "heavy", "general_local"),
)
