"""Knowledge graph reads (GRAPH-01/02/07/08) and relationship review (GRAPH-06).

Visibility is decided at query time, per entity, from the principal's live grants:
- company   departments and people (the directory every member already sees)
- projects  holders of engineering.project.read in the project's department, while
            the Projects module is on there
- finance   holders of finance.budget.read in a department with the Finance module
            (the same people who see the purchase-request list), plus the requester
- hr        holders of hr.leave.approve in a department with the HR module, plus the
            employee who asked for the leave
- it        people who may view access requests (IT proposers, security approvers)
- document  the document visibility rule of the search, evaluated against the live
            document row (so archiving hides it at once)
An edge is visible only when both endpoints are visible, and, for a document-derived
edge, the document is visible as well. Traversal steps only over visible edges, so a
hidden entity cuts every path through it and is never named.

Departments and people are hubs: a traversal may reach them but never continues
through them. Otherwise everything that shares a colleague or a department would
look "connected", which is a false path, not a relationship.

Bounds: depth <= 3 (neighbourhood) or <= 4 (path), at most 60 entities and 200
edges per answer, and a statement timeout of 2 s for every graph query.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import Connection, text
from psycopg.errors import QueryCanceled
from sqlalchemy.exc import OperationalError

from abteilo import audit
from abteilo.authz import Principal
from abteilo.db import transaction
from abteilo.deps import current_principal, load_department, tx
from abteilo.errors import AppError, Conflict, NotFound
from abteilo.graph_projection import project_company
from abteilo.modules import documents, it

router = APIRouter(prefix="/api", tags=["graph"])

MAX_NODES = 60
MAX_EDGES = 200
HUB_KINDS = frozenset({"person", "department"})
STATEMENT_TIMEOUT = "2000ms"
EntityKind = Literal["department", "project", "task", "person", "purchase_request", "leave_request",
                     "access_request", "document"]
Authority = Literal["record", "document_mention", "reviewed"]


class GraphTimeout(AppError):
    status_code, code, message = 503, "GRAPH_TIMEOUT", "The relationship query took too long. Try a smaller depth."


class GraphNode(BaseModel):
    id: UUID
    kind: EntityKind
    ref_id: UUID
    label: str
    status: str | None
    depth: int


class GraphEvidence(BaseModel):
    source_type: str
    source_id: UUID
    document_version_id: UUID | None
    quote: str | None


class GraphEdge(BaseModel):
    id: UUID
    src_id: UUID
    dst_id: UUID
    kind: str
    authority: Authority
    valid_from: datetime
    evidence: list[GraphEvidence]


class Neighbourhood(BaseModel):
    root_id: UUID
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    truncated: bool
    projected_at: datetime | None


class PathResult(BaseModel):
    found: bool
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    projected_at: datetime | None


# --- visibility -------------------------------------------------------------------------------
@dataclass(frozen=True)
class GraphAccess:
    user_id: UUID
    project_departments: list[str]
    finance: bool
    hr: bool
    it: bool
    principal: Principal
    doc_params: dict

    def documents(self, alias: str) -> str:
        return documents._visibility(self.principal, alias)[0]

    def params(self) -> dict:
        return {"g_uid": self.user_id, "g_proj": self.project_departments, "g_fin": self.finance, "g_hr": self.hr,
                "g_it": self.it, **self.doc_params}


def graph_access(conn: Connection, p: Principal) -> GraphAccess:
    modules: dict[str, set[UUID]] = {}
    for dept_id, module in conn.execute(text(
            "SELECT m.department_id, m.module_code FROM core.department_modules m"
            " JOIN core.departments d ON d.id = m.department_id WHERE d.status = 'active'")):
        modules.setdefault(module, set()).add(dept_id)
    _, params = documents._visibility(p)
    return GraphAccess(
        user_id=p.user_id,
        project_departments=[str(d) for d in modules.get("projects", set()) if p.can("engineering.project.read", d)],
        finance=any(p.can("finance.budget.read", d) for d in modules.get("finance", set())),
        hr=any(p.can("hr.leave.approve", d) for d in modules.get("hr", set())),
        it=any(it.can_view_access(p, d) for d in modules.get("it", set())),
        principal=p, doc_params=params,
    )


def _entity_visible(alias: str, access: GraphAccess) -> str:
    doc = access.documents("vd")
    return (
        # INT-08: a person is visible only while their membership and account are active, checked live at
        # read time, so a suspension hides them at once even if the projection has not run yet.
        f"({alias}.active AND ({alias}.kind <> 'person' OR EXISTS (SELECT 1 FROM core.memberships vm"
        f"     JOIN core.users vu ON vu.id = vm.user_id WHERE vm.company_id = {alias}.company_id"
        f"     AND vm.user_id = {alias}.ref_id AND vm.status = 'active' AND vu.status = 'active'))"
        f" AND ({alias}.vis_scope = 'company'"
        f" OR ({alias}.vis_scope = 'projects' AND {alias}.vis_department_id = ANY(CAST(:g_proj AS uuid[])))"
        f" OR ({alias}.vis_scope = 'finance' AND :g_fin)"
        f" OR ({alias}.vis_scope = 'hr' AND :g_hr)"
        f" OR ({alias}.vis_scope = 'it' AND :g_it)"
        f" OR ({alias}.vis_scope = 'document' AND EXISTS (SELECT 1 FROM core.documents vd"
        f"     WHERE vd.id = {alias}.ref_id AND {doc}))"
        f" OR {alias}.vis_user_id = :g_uid))"
    )


def _edge_visible(access: GraphAccess) -> str:
    doc = access.documents("ed")
    return (f"r.valid_to IS NULL AND {_entity_visible('s', access)} AND {_entity_visible('t', access)}"
            f" AND (r.document_id IS NULL OR EXISTS (SELECT 1 FROM core.documents ed WHERE ed.id = r.document_id AND {doc}))")


def _bounded(conn: Connection) -> None:
    conn.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))


def _timed_out(exc: OperationalError) -> bool:
    return isinstance(exc.orig, QueryCanceled)


_NODE_COLUMNS = "e.id, e.kind, e.ref_id, e.label, e.status"


def find_entity(conn: Connection, access: GraphAccess, kind: str, ref_id: UUID) -> dict:
    """The same NotFound for a missing and a hidden entity: no existence oracle."""
    row = conn.execute(text(f"SELECT {_NODE_COLUMNS} FROM core.graph_entities e"
                            f" WHERE e.kind = :k AND e.ref_id = :r AND {_entity_visible('e', access)}"),
                       {"k": kind, "r": ref_id, **access.params()}).mappings().first()
    if row is None:
        raise NotFound()
    return dict(row)


def _entity_by_id(conn: Connection, access: GraphAccess, entity_id: UUID) -> dict:
    row = conn.execute(text(f"SELECT {_NODE_COLUMNS} FROM core.graph_entities e"
                            f" WHERE e.id = :id AND {_entity_visible('e', access)}"),
                       {"id": entity_id, **access.params()}).mappings().first()
    if row is None:
        raise NotFound()
    return dict(row)


def _expand(conn: Connection, access: GraphAccess, frontier: list[UUID]) -> list[dict]:
    """All visible current edges touching the frontier, with the entity on the other side."""
    return [dict(r) for r in conn.execute(text(
        "SELECT r.id, r.src_id, r.dst_id, r.kind, r.authority, r.valid_from,"
        " s.kind AS src_kind, s.ref_id AS src_ref, s.label AS src_label, s.status AS src_status,"
        " t.kind AS dst_kind, t.ref_id AS dst_ref, t.label AS dst_label, t.status AS dst_status"
        " FROM core.graph_relationships r"
        " JOIN core.graph_entities s ON s.id = r.src_id JOIN core.graph_entities t ON t.id = r.dst_id"
        f" WHERE (r.src_id = ANY(CAST(:f AS uuid[])) OR r.dst_id = ANY(CAST(:f AS uuid[]))) AND {_edge_visible(access)}"
        " ORDER BY r.kind, r.id LIMIT :n"),
        {"f": [str(x) for x in frontier], "n": MAX_EDGES + 1, **access.params()}).mappings()]


def _evidence(conn: Connection, edge_ids: list[UUID]) -> dict[UUID, list[GraphEvidence]]:
    out: dict[UUID, list[GraphEvidence]] = {}
    if edge_ids:
        for r in conn.execute(text(
                "SELECT relationship_id, source_type, source_id, document_version_id, quote FROM core.graph_evidence"
                " WHERE relationship_id = ANY(CAST(:ids AS uuid[])) ORDER BY created_at"),
                {"ids": [str(x) for x in edge_ids]}).mappings():
            out.setdefault(r["relationship_id"], []).append(GraphEvidence(**{k: r[k] for k in GraphEvidence.model_fields}))
    return out


def _projected_at(conn: Connection) -> datetime | None:
    return conn.execute(text("SELECT last_finished_at FROM core.graph_projection_state")).scalar()


def _edge_view(row: dict, evidence: dict[UUID, list[GraphEvidence]]) -> GraphEdge:
    return GraphEdge(id=row["id"], src_id=row["src_id"], dst_id=row["dst_id"], kind=row["kind"],
                     authority=row["authority"], valid_from=row["valid_from"], evidence=evidence.get(row["id"], []))


def neighbourhood(conn: Connection, p: Principal, kind: str, ref_id: UUID, depth: int = 2) -> Neighbourhood:
    _bounded(conn)
    access = graph_access(conn, p)
    root = find_entity(conn, access, kind, ref_id)
    nodes = {root["id"]: GraphNode(**root, depth=0)}
    edges: dict[UUID, dict] = {}
    frontier = [root["id"]]
    truncated = False
    try:  # entity lookups are single-row index reads; the expansion is what the timeout bounds
        for level in range(1, min(depth, 3) + 1):
            expandable = [n for n in frontier if n == root["id"] or nodes[n].kind not in HUB_KINDS]
            if not expandable:
                break
            rows = _expand(conn, access, expandable)
            truncated |= len(rows) > MAX_EDGES
            nxt = []
            for r in rows[:MAX_EDGES]:
                for side in ("src", "dst"):
                    node_id = r[f"{side}_id"]
                    if node_id not in nodes:
                        if len(nodes) >= MAX_NODES:
                            truncated = True
                            break
                        nodes[node_id] = GraphNode(id=node_id, kind=r[f"{side}_kind"], ref_id=r[f"{side}_ref"],
                                                   label=r[f"{side}_label"], status=r[f"{side}_status"], depth=level)
                        nxt.append(node_id)
                if r["src_id"] in nodes and r["dst_id"] in nodes and len(edges) < MAX_EDGES:
                    edges[r["id"]] = r
            frontier = nxt
        evidence = _evidence(conn, list(edges))
    except OperationalError as exc:
        if _timed_out(exc):
            raise GraphTimeout() from exc
        raise
    return Neighbourhood(root_id=root["id"], nodes=list(nodes.values()),
                         edges=[_edge_view(e, evidence) for e in edges.values()], truncated=truncated,
                         projected_at=_projected_at(conn))


def path(conn: Connection, p: Principal, source_id: UUID, target_id: UUID, max_depth: int = 4) -> PathResult:
    """Shortest visible path (breadth-first, hubs not passed through)."""
    _bounded(conn)
    access = graph_access(conn, p)
    source, target = _entity_by_id(conn, access, source_id), _entity_by_id(conn, access, target_id)
    parents: dict[UUID, tuple[UUID, dict] | None] = {source["id"]: None}
    info = {source["id"]: (source, 0)}
    frontier = [source["id"]]
    try:
        for level in range(1, min(max_depth, 4) + 1):
            expandable = [n for n in frontier if n == source["id"] or info[n][0]["kind"] not in HUB_KINDS]
            if not expandable or target["id"] in parents:
                break
            nxt = []
            for r in _expand(conn, access, expandable)[:MAX_EDGES]:
                for here, there in (("src", "dst"), ("dst", "src")):
                    if r[f"{here}_id"] in expandable and r[f"{there}_id"] not in parents and len(parents) < MAX_NODES:
                        node_id = r[f"{there}_id"]
                        parents[node_id] = (r[f"{here}_id"], r)
                        info[node_id] = ({"id": node_id, "kind": r[f"{there}_kind"], "ref_id": r[f"{there}_ref"],
                                          "label": r[f"{there}_label"], "status": r[f"{there}_status"]}, level)
                        nxt.append(node_id)
            frontier = nxt
        if target["id"] not in parents:
            return PathResult(found=False, nodes=[], edges=[], projected_at=_projected_at(conn))
        chain_nodes, chain_edges = [target["id"]], []
        while parents[chain_nodes[-1]] is not None:
            parent, edge = parents[chain_nodes[-1]]
            chain_edges.append(edge)
            chain_nodes.append(parent)
        chain_nodes.reverse()
        chain_edges.reverse()
        evidence = _evidence(conn, [e["id"] for e in chain_edges])
    except OperationalError as exc:
        if _timed_out(exc):
            raise GraphTimeout() from exc
        raise
    return PathResult(found=True, nodes=[GraphNode(**info[n][0], depth=info[n][1]) for n in chain_nodes],
                      edges=[_edge_view(e, evidence) for e in chain_edges], projected_at=_projected_at(conn))


# --- relationship review (GRAPH-06) ------------------------------------------------------------
class CandidateEntity(BaseModel):
    id: UUID
    kind: EntityKind
    label: str


class CandidateView(BaseModel):
    id: UUID
    document_id: UUID
    document_title: str
    version: int
    source: CandidateEntity
    target: CandidateEntity
    kind: Literal["depends_on", "blocked_by", "delays", "affects"]
    quote: str
    extractor: str
    created_at: datetime


class CandidateDecision(BaseModel):
    decision: Literal["accept", "reject"]


class DecisionResult(BaseModel):
    id: UUID
    status: Literal["accepted", "rejected"]
    relationship_id: UUID | None


_CANDIDATE_SQL = """
SELECT c.id, c.document_id, d.title AS document_title, v.version, c.kind, c.quote, c.extractor, c.created_at,
       c.status, c.document_version_id, c.src_id, c.dst_id, d.department_id,
       s.kind AS s_kind, s.label AS s_label, t.kind AS t_kind, t.label AS t_label
FROM core.graph_candidates c
JOIN core.documents d ON d.id = c.document_id
JOIN core.document_versions v ON v.id = c.document_version_id AND v.version = d.current_version
JOIN core.graph_entities s ON s.id = c.src_id
JOIN core.graph_entities t ON t.id = c.dst_id
WHERE c.status = 'proposed' AND {where}
"""


def _candidate_view(r) -> CandidateView:
    return CandidateView(id=r["id"], document_id=r["document_id"], document_title=r["document_title"],
                         version=r["version"], kind=r["kind"], quote=r["quote"], extractor=r["extractor"],
                         created_at=r["created_at"], source=CandidateEntity(id=r["src_id"], kind=r["s_kind"], label=r["s_label"]),
                         target=CandidateEntity(id=r["dst_id"], kind=r["t_kind"], label=r["t_label"]))


def _reviewable(access: GraphAccess) -> str:
    doc = access.documents("d")  # the candidate's document must be visible to the reviewer
    return f"{doc} AND {_entity_visible('s', access)} AND {_entity_visible('t', access)}"


def list_candidates(conn: Connection, p: Principal, department_id: UUID) -> list[CandidateView]:
    """Editors of a department review suggestions from that department's documents. They see only
    suggestions whose document and both entities they may already see."""
    p.require("documents.write", department_id)
    access = graph_access(conn, p)
    rows = conn.execute(text(_CANDIDATE_SQL.format(where=f"d.department_id = :dept AND {_reviewable(access)}")
                             + " ORDER BY c.created_at LIMIT 100"),
                        {"dept": department_id, **access.params()}).mappings()
    return [_candidate_view(r) for r in rows]


def decide_candidate(conn: Connection, p: Principal, candidate_id: UUID, decision: str) -> DecisionResult:
    access = graph_access(conn, p)
    locked = conn.execute(text("SELECT status FROM core.graph_candidates WHERE id = :id FOR UPDATE"),
                          {"id": candidate_id}).first()
    if locked is None:
        raise NotFound()
    row = conn.execute(text(_CANDIDATE_SQL.format(where=f"c.id = :id AND {_reviewable(access)}")),
                       {"id": candidate_id, **access.params()}).mappings().first()
    if row is None:
        if locked.status != "proposed":
            raise Conflict("This suggestion was already decided or its document has changed.")
        raise NotFound()
    p.require("documents.write", row["department_id"])
    relationship_id = None
    if decision == "accept":
        relationship_id = uuid4()
        inserted = conn.execute(text(
            "INSERT INTO core.graph_relationships (id, company_id, src_id, dst_id, kind, authority, document_id)"
            " VALUES (:id, core.current_company_id(), :s, :d, :k, 'reviewed', :doc) ON CONFLICT DO NOTHING RETURNING id"),
            {"id": relationship_id, "s": row["src_id"], "d": row["dst_id"], "k": row["kind"], "doc": row["document_id"]}).first()
        if inserted is None:
            raise Conflict("This relationship already exists.")
        conn.execute(text(
            "INSERT INTO core.graph_evidence (id, company_id, relationship_id, source_type, source_id, document_version_id, quote)"
            " VALUES (:id, core.current_company_id(), :r, 'document_version', :v, :v, :q)"),
            {"id": uuid4(), "r": relationship_id, "v": row["document_version_id"], "q": row["quote"]})
    status = "accepted" if decision == "accept" else "rejected"
    conn.execute(text("UPDATE core.graph_candidates SET status = :s, reviewed_by = :u, reviewed_at = now(),"
                      " relationship_id = :r WHERE id = :id"),
                 {"s": status, "u": p.user_id, "r": relationship_id, "id": candidate_id})
    audit.record(conn, p.company_id, p.user_id, f"graph.relationship_{status}", "graph_candidate", candidate_id,
                 {"kind": row["kind"], "document_id": str(row["document_id"])})
    return DecisionResult(id=candidate_id, status=status, relationship_id=relationship_id)


# --- read audit (INT-09) -------------------------------------------------------------------------
def log_read(p: Principal, operation: str, depth: int, outcome: str, *, root_kind: str | None = None,
             root: UUID | None = None, target: UUID | None = None, nodes: int = 0, request_id: str | None = None) -> None:
    """Metadata only, in its own short transaction, so denied and timed-out reads are recorded too.
    `root`/`target` are the identifiers the caller asked for (record ref ids, or entity ids for a path)."""
    with transaction(p.company_id, p.user_id) as conn:
        conn.execute(text(
            "INSERT INTO core.graph_read_log (company_id, actor_user_id, request_id, operation, root_kind, root_ref_id,"
            " target_ref_id, depth, outcome, node_count)"
            " VALUES (core.current_company_id(), :u, :r, :op, :k, :root, :t, :d, :o, :n)"),
            {"u": p.user_id, "r": request_id, "op": operation, "k": root_kind, "root": root, "t": target,
             "d": depth, "o": outcome, "n": min(nodes, 1000)})


def _logged(p: Principal, request: Request, operation: str, depth: int, run, **ids):
    try:
        result = run()
    except NotFound:
        log_read(p, operation, depth, "not_found", request_id=request.state.request_id, **ids)
        raise
    except GraphTimeout:
        log_read(p, operation, depth, "timeout", request_id=request.state.request_id, **ids)
        raise
    log_read(p, operation, depth, "ok", nodes=len(result.nodes), request_id=request.state.request_id, **ids)
    return result


# --- routes ------------------------------------------------------------------------------------
@router.get("/graph/{kind}/{ref_id}/neighbourhood", response_model=Neighbourhood)
def api_neighbourhood(kind: EntityKind, ref_id: UUID, request: Request, depth: int = Query(2, ge=1, le=3),
                      p: Principal = Depends(current_principal)):
    def run():
        with tx(p) as conn:
            return neighbourhood(conn, p, kind, ref_id, depth)
    return _logged(p, request, "neighbourhood", depth, run, root_kind=kind, root=ref_id)


@router.get("/graph/path", response_model=PathResult)
def api_path(source: UUID, target: UUID, request: Request, max_depth: int = Query(4, ge=1, le=4),
             p: Principal = Depends(current_principal)):
    def run():
        with tx(p) as conn:
            return path(conn, p, source, target, max_depth)
    return _logged(p, request, "path", max_depth, run, root=source, target=target)


@router.get("/departments/{department_id}/graph/candidates", response_model=list[CandidateView])
def api_candidates(department_id: UUID, p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        load_department(conn, department_id, "documents")
        return list_candidates(conn, p, department_id)


@router.post("/graph/candidates/{candidate_id}/decision", response_model=DecisionResult)
def api_decide(candidate_id: UUID, body: CandidateDecision, p: Principal = Depends(current_principal)):
    with tx(p) as conn:
        return decide_candidate(conn, p, candidate_id, body.decision)


# --- owner console: health and counts only (SEC-04: no record content) --------------------------
class GraphStatus(BaseModel):
    last_started_at: datetime | None
    last_finished_at: datetime | None
    last_status: Literal["ok", "failed"] | None
    last_error_code: str | None
    runs: int
    entities: dict[str, int]          # kind -> active entities
    relationships: dict[str, int]     # authority -> current edges
    ended_relationships: int
    candidates: dict[str, int]        # status -> suggestions
    extraction_failed: int
    extraction_errors: dict[str, int]


class RebuildResult(BaseModel):
    entities_changed: int
    edges_added: int
    edges_ended: int
    candidates_superseded: int


@router.get("/admin/graph", response_model=GraphStatus)
def api_graph_status(p: Principal = Depends(current_principal)):
    p.require("company.settings.manage")
    with tx(p) as conn:
        state = conn.execute(text("SELECT last_started_at, last_finished_at, last_status, last_error_code, runs"
                                  " FROM core.graph_projection_state")).mappings().first() or {
            "last_started_at": None, "last_finished_at": None, "last_status": None, "last_error_code": None, "runs": 0}
        return GraphStatus(
            **state,
            entities=dict(conn.execute(text("SELECT kind, count(*) FROM core.graph_entities WHERE active GROUP BY 1")).all()),
            relationships=dict(conn.execute(text(
                "SELECT authority, count(*) FROM core.graph_relationships WHERE valid_to IS NULL GROUP BY 1")).all()),
            ended_relationships=conn.execute(text(
                "SELECT count(*) FROM core.graph_relationships WHERE valid_to IS NOT NULL")).scalar_one(),
            candidates=dict(conn.execute(text("SELECT status, count(*) FROM core.graph_candidates GROUP BY 1")).all()),
            extraction_failed=conn.execute(text(
                "SELECT count(*) FROM core.graph_extraction_runs WHERE status = 'failed'")).scalar_one(),
            extraction_errors=dict(conn.execute(text(
                "SELECT error_code, count(*) FROM core.graph_extraction_runs WHERE status = 'failed' GROUP BY 1")).all()),
        )


@router.post("/admin/graph/rebuild", response_model=RebuildResult)
def api_graph_rebuild(p: Principal = Depends(current_principal)):
    """Run the projection now instead of waiting for the worker. Idempotent."""
    p.require("company.settings.manage")
    run = project_company(p.company_id)
    with tx(p) as conn:
        audit.record(conn, p.company_id, p.user_id, "graph.rebuilt", "company", p.company_id, {"changes": run.changes})
    return RebuildResult(entities_changed=run.entities_changed, edges_added=run.edges_added,
                         edges_ended=run.edges_ended, candidates_superseded=run.candidates_superseded)
