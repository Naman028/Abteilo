"""Graph projection (GRAPH-03/04/05): rebuild the company graph from its records.

The graph is derived data. Business tables stay the source of truth; this job only
reads them and writes the graph tables. One run per company is one short
transaction that holds the company's projection-state row lock, so concurrent
workers serialise instead of racing. A run is idempotent: it computes the desired
entities and edges, inserts what is missing, updates what changed and ends edges
whose source no longer supports them. A second run with no new records changes
nothing.

Edges come only from facts the database already holds:
- `record`: a foreign key in a business table (task -> project, request -> person, ...);
- `document_mention`: a project code that literally occurs in the current version of
  a document (evidence: that version and the sentence around the code).
Relationships a model suggests from document text are never projected here; they
wait in graph_candidates for a reviewer (GRAPH-06).
"""

import logging
import re
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import Connection, text

from abteilo.db import transaction

log = logging.getLogger("abteilo.graph")
QUOTE_CHARS = 240

# Entities: (kind, SQL producing ref_id, label, status, department_id, vis_scope, vis_department_id, vis_user_id, active)
_ENTITY_SQL = {
    "department": """
        SELECT id AS ref_id, name AS label, status, id AS department_id, 'company' AS vis_scope,
               NULL::uuid AS vis_department_id, NULL::uuid AS vis_user_id, status = 'active' AS active
        FROM core.departments""",
    "project": """
        SELECT p.id, p.code || ' ' || p.name, p.status, p.department_id, 'projects', p.department_id, NULL::uuid,
               p.status <> 'archived' AND d.status = 'active'
        FROM core.projects p JOIN core.departments d ON d.id = p.department_id""",
    "task": """
        SELECT t.id, t.title, t.status, p.department_id, 'projects', p.department_id, NULL::uuid,
               p.status <> 'archived' AND d.status = 'active'
        FROM core.tasks t JOIN core.projects p ON p.id = t.project_id JOIN core.departments d ON d.id = p.department_id""",
    "person": """
        SELECT u.id, u.display_name, m.status, NULL::uuid, 'company', NULL::uuid, NULL::uuid,
               m.status = 'active' AND u.status = 'active'
        FROM core.memberships m JOIN core.users u ON u.id = m.user_id
        WHERE m.company_id = core.current_company_id()""",  # GRAPH-11: memberships and users have no RLS, scope explicitly
    "purchase_request": """
        SELECT id, title, status, department_id, 'finance', NULL::uuid, requested_by, true
        FROM core.purchase_requests""",
    "leave_request": """
        SELECT id, 'Leave ' || to_char(start_date, 'YYYY-MM-DD') || ' to ' || to_char(end_date, 'YYYY-MM-DD'), status,
               NULL::uuid, 'hr', NULL::uuid, user_id, true
        FROM core.leave_requests""",
    "access_request": """
        SELECT id, 'Role ' || role_code, status, department_id, 'it', NULL::uuid, NULL::uuid, true
        FROM core.access_requests""",
    "document": """
        SELECT id, title, status, department_id, 'document', department_id, NULL::uuid, status = 'active'
        FROM core.documents""",
}

# Record edges: (src kind, src ref, dst kind, dst ref, edge kind, evidence source type, evidence source id)
_RECORD_EDGES_SQL = """
SELECT 'department' AS sk, department_id AS sr, 'project' AS dk, id AS dr, 'owns' AS kind, 'project' AS st, id AS sid
  FROM core.projects
UNION ALL SELECT 'project', project_id, 'task', id, 'has_task', 'task', id FROM core.tasks
UNION ALL SELECT 'task', id, 'person', assignee_id, 'assigned_to', 'task', id FROM core.tasks WHERE assignee_id IS NOT NULL
UNION ALL SELECT 'person', user_id, 'project', project_id, 'member_of', 'project', project_id FROM core.project_members
UNION ALL SELECT 'purchase_request', id, 'department', department_id, 'requested_in', 'purchase_request', id
  FROM core.purchase_requests
UNION ALL SELECT 'purchase_request', id, 'project', project_id, 'requested_for', 'purchase_request', id
  FROM core.purchase_requests WHERE project_id IS NOT NULL
UNION ALL SELECT 'purchase_request', id, 'person', requested_by, 'requested_by', 'purchase_request', id
  FROM core.purchase_requests
UNION ALL SELECT 'purchase_request', id, 'person', decided_by, 'decided_by', 'purchase_request', id
  FROM core.purchase_requests WHERE decided_by IS NOT NULL
UNION ALL SELECT 'leave_request', id, 'person', user_id, 'requested_by', 'leave_request', id FROM core.leave_requests
UNION ALL SELECT 'leave_request', id, 'person', decided_by, 'decided_by', 'leave_request', id
  FROM core.leave_requests WHERE decided_by IS NOT NULL
UNION ALL SELECT 'access_request', id, 'person', target_user_id, 'targets', 'access_request', id FROM core.access_requests
UNION ALL SELECT 'access_request', id, 'person', proposed_by, 'proposed_by', 'access_request', id FROM core.access_requests
UNION ALL SELECT 'access_request', id, 'person', decided_by, 'decided_by', 'access_request', id
  FROM core.access_requests WHERE decided_by IS NOT NULL
UNION ALL SELECT 'access_request', id, 'department', department_id, 'for_department', 'access_request', id
  FROM core.access_requests WHERE department_id IS NOT NULL
"""


@dataclass(frozen=True)
class ProjectionRun:
    entities_changed: int = 0
    edges_added: int = 0
    edges_ended: int = 0
    candidates_superseded: int = 0

    @property
    def changes(self) -> int:
        return self.entities_changed + self.edges_added + self.edges_ended + self.candidates_superseded


@dataclass(frozen=True)
class _Edge:
    src: UUID
    dst: UUID
    kind: str
    authority: str
    source_type: str
    source_id: UUID
    document_id: UUID | None = None
    quote: str | None = None

    @property
    def key(self) -> tuple:
        # The evidence source is part of the key: a mention backed by an older document version
        # is ended and re-created against the new version, so evidence always points at what is current.
        return (self.src, self.dst, self.kind, self.source_type, self.source_id, self.quote)


_ENTITY_COLUMNS = "ref_id, label, status, department_id, vis_scope, vis_department_id, vis_user_id, active"


def _upsert_entities(conn: Connection) -> int:
    changed = 0
    for kind, sql in _ENTITY_SQL.items():
        changed += conn.execute(text(f"""
            INSERT INTO core.graph_entities (id, company_id, kind, ref_id, label, status, department_id, vis_scope,
                                             vis_department_id, vis_user_id, active)
            SELECT gen_random_uuid(), core.current_company_id(), :kind, s.* FROM ({sql}) s
            ON CONFLICT (company_id, kind, ref_id) WHERE quarantine_reason IS NULL DO UPDATE SET
                label = EXCLUDED.label, status = EXCLUDED.status, department_id = EXCLUDED.department_id,
                vis_scope = EXCLUDED.vis_scope, vis_department_id = EXCLUDED.vis_department_id,
                vis_user_id = EXCLUDED.vis_user_id, active = EXCLUDED.active, projected_at = now()
            WHERE (core.graph_entities.label, core.graph_entities.status, core.graph_entities.department_id,
                   core.graph_entities.vis_scope, core.graph_entities.vis_department_id,
                   core.graph_entities.vis_user_id, core.graph_entities.active)
                  IS DISTINCT FROM (EXCLUDED.label, EXCLUDED.status, EXCLUDED.department_id, EXCLUDED.vis_scope,
                                    EXCLUDED.vis_department_id, EXCLUDED.vis_user_id, EXCLUDED.active)
        """), {"kind": kind}).rowcount
        # An entity whose source row is no longer produced (or never belonged here) stops being visible.
        changed += conn.execute(text(f"""
            UPDATE core.graph_entities SET active = false, projected_at = now()
            WHERE kind = :kind AND active AND ref_id NOT IN (SELECT s.ref_id FROM ({sql}) AS s({_ENTITY_COLUMNS}))
        """), {"kind": kind}).rowcount
    return changed


def _quote_around(content: str, start: int, end: int) -> str:
    """The sentence (bounded) that contains the match: enough for a reader to check the mention."""
    sentence = content.rfind(". ", 0, start)
    left = max(sentence + 2 if sentence != -1 else 0, content.rfind("\n", 0, start) + 1, start - QUOTE_CHARS // 2, 0)
    stops = [i for i in (content.find(". ", end), content.find("\n", end)) if i != -1]
    right = min([*(i + 1 for i in stops), end + QUOTE_CHARS // 2, len(content)])
    return content[left:right].strip()[:QUOTE_CHARS]


def _desired_edges(conn: Connection) -> list[_Edge]:
    ids = {(r.kind, r.ref_id): (r.id, r.active) for r in conn.execute(
        text("SELECT id, kind, ref_id, active FROM core.graph_entities WHERE quarantine_reason IS NULL"))}
    edges: list[_Edge] = []

    def add(sk, sr, dk, dr, kind, authority, st, sid, document_id=None, quote=None):
        src, dst = ids.get((sk, sr)), ids.get((dk, dr))
        if src and dst and src[1] and dst[1]:  # only between active entities
            edges.append(_Edge(src[0], dst[0], kind, authority, st, sid, document_id, quote))

    for r in conn.execute(text(_RECORD_EDGES_SQL)):
        add(r.sk, r.sr, r.dk, r.dr, r.kind, "record", r.st, r.sid)

    for r in conn.execute(text("SELECT id, department_id FROM core.documents")):
        add("department", r.department_id, "document", r.id, "owns", "record", "document", r.id)
    projects = conn.execute(text("SELECT id, code FROM core.projects")).all()
    docs = conn.execute(text(
        "SELECT d.id, d.department_id, v.id AS version_id, v.content FROM core.documents d"
        " JOIN core.document_versions v ON v.document_id = d.id AND v.version = d.current_version")).all()
    for doc in docs:
        for project in projects:
            match = re.search(rf"(?<![A-Za-z0-9-]){re.escape(project.code)}(?![A-Za-z0-9-])", doc.content)
            if match:
                add("document", doc.id, "project", project.id, "mentions", "document_mention", "document_version",
                    doc.version_id, doc.id, _quote_around(doc.content, match.start(), match.end()))
    return edges


def _insert_edge(conn: Connection, edge: _Edge) -> None:
    rel_id = uuid4()
    conn.execute(text(
        "INSERT INTO core.graph_relationships (id, company_id, src_id, dst_id, kind, authority, document_id)"
        " VALUES (:id, core.current_company_id(), :s, :d, :k, :a, :doc)"),
        {"id": rel_id, "s": edge.src, "d": edge.dst, "k": edge.kind, "a": edge.authority, "doc": edge.document_id})
    conn.execute(text(
        "INSERT INTO core.graph_evidence (id, company_id, relationship_id, source_type, source_id, document_version_id, quote)"
        " VALUES (:id, core.current_company_id(), :r, :st, :sid, :dv, :q)"),
        {"id": uuid4(), "r": rel_id, "st": edge.source_type, "sid": edge.source_id,
         "dv": edge.source_id if edge.source_type == "document_version" else None, "q": edge.quote})


def _end(conn: Connection, relationship_id: UUID, reason: str) -> int:
    # A clock stepped back (NTP, WSL resync) must not end an edge before it started.
    return conn.execute(text(
        "UPDATE core.graph_relationships SET valid_to = greatest(now(), valid_from), ended_reason = :r"
        " WHERE id = :id AND valid_to IS NULL"),
        {"id": relationship_id, "r": reason}).rowcount


def _sync_edges(conn: Connection) -> tuple[int, int]:
    desired: dict[tuple, _Edge] = {}
    seen: set[tuple] = set()
    for edge in _desired_edges(conn):
        if (edge.src, edge.dst, edge.kind) not in seen:  # one current edge per (src, dst, kind)
            seen.add((edge.src, edge.dst, edge.kind))
            desired[edge.key] = edge
    active = {r.id: r.active for r in conn.execute(text("SELECT id, active FROM core.graph_entities"))}
    current = conn.execute(text(
        "SELECT r.id, r.src_id, r.dst_id, r.kind, e.source_type, e.source_id, e.quote FROM core.graph_relationships r"
        " JOIN core.graph_evidence e ON e.relationship_id = r.id"
        " WHERE r.valid_to IS NULL AND r.authority IN ('record', 'document_mention')")).all()
    ended = 0
    current_keys = set()
    for r in current:
        key = (r.src_id, r.dst_id, r.kind, r.source_type, r.source_id, r.quote)
        if key in desired:
            current_keys.add(key)
            continue
        if not (active.get(r.src_id) and active.get(r.dst_id)):
            reason = "source_archived"
        elif r.source_type == "document_version":
            reason = "document_superseded"
        else:
            reason = "source_changed"
        ended += _end(conn, r.id, reason)
    added = 0
    for key, edge in desired.items():
        if key not in current_keys:
            _insert_edge(conn, edge)
            added += 1
    return added, ended


def _sync_reviewed(conn: Connection) -> tuple[int, int]:
    """Reviewed edges and open candidates are bound to one document version; a new version
    or an archived document ends them (they must be reviewed again against current text)."""
    ended = 0
    for r in conn.execute(text("""
        SELECT r.id, (s.active AND t.active) AS endpoints_active, d.status = 'active' AS doc_active,
               v.version = d.current_version AS is_current
        FROM core.graph_relationships r
        JOIN core.graph_evidence e ON e.relationship_id = r.id
        JOIN core.document_versions v ON v.id = e.document_version_id
        JOIN core.documents d ON d.id = r.document_id
        JOIN core.graph_entities s ON s.id = r.src_id JOIN core.graph_entities t ON t.id = r.dst_id
        WHERE r.valid_to IS NULL AND r.authority = 'reviewed'""")).all():
        if not (r.endpoints_active and r.doc_active):
            ended += _end(conn, r.id, "source_archived")
        elif not r.is_current:
            ended += _end(conn, r.id, "document_superseded")
    superseded = conn.execute(text("""
        UPDATE core.graph_candidates c SET status = 'superseded'
        FROM core.document_versions v, core.documents d
        WHERE c.status = 'proposed' AND v.id = c.document_version_id AND d.id = c.document_id
          AND (v.version <> d.current_version OR d.status <> 'active')""")).rowcount
    return ended, superseded


def project_company(company_id: UUID) -> ProjectionRun:
    """One idempotent run, serialised per company by the projection-state row lock."""
    try:
        with transaction(company_id) as conn:
            conn.execute(text("INSERT INTO core.graph_projection_state (company_id) VALUES (core.current_company_id())"
                              " ON CONFLICT (company_id) DO NOTHING"))
            conn.execute(text("SELECT 1 FROM core.graph_projection_state FOR UPDATE"))
            conn.execute(text("UPDATE core.graph_projection_state SET last_started_at = now()"))
            entities = _upsert_entities(conn)
            added, ended = _sync_edges(conn)
            reviewed_ended, superseded = _sync_reviewed(conn)
            run = ProjectionRun(entities, added, ended + reviewed_ended, superseded)
            conn.execute(text(
                "UPDATE core.graph_projection_state SET last_finished_at = now(), last_status = 'ok',"
                " last_error_code = NULL, last_changes = :n, runs = runs + 1"), {"n": run.changes})
            return run
    except Exception:
        # The run rolled back as a whole: the graph keeps its previous state, business data was only read.
        with transaction(company_id) as conn:
            conn.execute(text(
                "INSERT INTO core.graph_projection_state (company_id, last_started_at, last_status, last_error_code)"
                " VALUES (core.current_company_id(), now(), 'failed', 'GRAPH_PROJECTION_FAILED')"
                " ON CONFLICT (company_id) DO UPDATE SET last_status = 'failed', last_error_code = 'GRAPH_PROJECTION_FAILED'"))
        raise


def project_all() -> ProjectionRun:
    with transaction() as conn:
        companies = conn.execute(text("SELECT DISTINCT company_id FROM core.memberships")).scalars().all()
    total = ProjectionRun()
    for company_id in companies:
        try:
            run = project_company(company_id)
        except Exception:
            log.exception('{"event":"graph_projection_failed"}')
            continue
        total = ProjectionRun(total.entities_changed + run.entities_changed, total.edges_added + run.edges_added,
                              total.edges_ended + run.edges_ended, total.candidates_superseded + run.candidates_superseded)
    return total
