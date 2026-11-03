"""Relationship evidence for the assistant (Block D, D4).

`graph.related(subject)` resolves the subject to an entity the principal may see (never by the model:
only labels of visible entities match), reads its neighbourhood under the same visibility rules as the
graph API (GRAPH-01/02/08, INT-08), and re-reads the current status of every returned record from its
business table. The graph is navigation; current state comes from the live rows (INT-05).

Each read is written to the graph-read log (INT-09). A timeout degrades to "no relationship evidence"
inside a savepoint, so it never aborts the other tools of the same request.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from abteilo import graph
from abteilo.ai.evidence import CAUSAL_EDGE_KINDS, EdgeFact
from abteilo.errors import NotFound

STALE_AFTER = timedelta(minutes=10)
MAX_EDGES = 15
MAX_CHARS = 1500
# Kinds whose current status lives in a business table (re-read live); departments and people are hubs.
LIVE_TABLES = {"project": "core.projects", "task": "core.tasks", "purchase_request": "core.purchase_requests",
               "leave_request": "core.leave_requests", "access_request": "core.access_requests",
               "document": "core.documents"}


class GraphDegraded(Exception):
    """The graph could not answer within its bounds; the request continues without relationship evidence."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RelatedResult:
    root_label: str
    content: str
    edges: tuple[EdgeFact, ...]
    stale: bool
    projected_at: datetime | None


def _like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Kind words a planner wraps around a name ("project A17", "the thermal test rig purchase", "Aufgabe ...").
_KIND_WORDS = re.compile(r"\b(the|a|an|der|die|das|project|projekt|task|aufgabe|purchase|purchase request|request|"
                         r"anfrage|antrag|document|dokument|record)\b", re.I)


def _normalise(subject: str) -> str:
    return re.sub(r"\s+", " ", _KIND_WORDS.sub(" ", subject)).strip(" .,:;'\"“”")


def _resolve(conn, access: graph.GraphAccess, subject: str):
    """Best visible match for a subject the planner names. Only labels the principal may see are candidates.

    1. The subject as written, then normalised (kind words removed: "project A17" -> "A17"): exact label, label
       starting with it (project codes), label containing it.
    2. Otherwise the longest visible label contained in the subject ("thermal test rig purchase" -> "Thermal test
       rig"). If two different records share that label, nothing is resolved: an ambiguous name identifies none.
    This is discovery only; whether a claim about the result is verified is decided by the verifier."""
    for s in dict.fromkeys([subject.strip(), _normalise(subject)]):
        if len(s) >= 2 and (row := _resolve_one(conn, access, s)) is not None:
            return row
    s = _normalise(subject)
    if len(s) < 4:
        return None
    rows = conn.execute(text(
        f"SELECT e.kind, e.ref_id, e.label FROM core.graph_entities e WHERE {graph._entity_visible('e', access)}"
        # Records only: departments and people are hubs, never resolved from a phrase ("engineering tasks" must
        # not become the Engineering department; F5, 2026-10-23).
        " AND e.kind NOT IN ('department', 'person')"
        " AND length(e.label) >= 4 AND position(lower(e.label) in lower(:s)) > 0"
        " ORDER BY length(e.label) DESC, e.id LIMIT 5"), {"s": s, **access.params()}).all()
    if not rows:
        return None
    longest = [r for r in rows if len(r.label) == len(rows[0].label)]
    return rows[0] if len({(r.kind, r.ref_id) for r in longest}) == 1 else None


def _resolve_one(conn, access: graph.GraphAccess, s: str):
    return conn.execute(text(
        f"SELECT e.kind, e.ref_id, e.label FROM core.graph_entities e WHERE {graph._entity_visible('e', access)}"
        " AND (lower(e.label) = lower(:s) OR lower(e.label) LIKE lower(:p) || ' %' ESCAPE '\\'"
        "      OR lower(e.label) LIKE '%' || lower(:p) || '%' ESCAPE '\\')"
        " ORDER BY lower(e.label) = lower(:s) DESC, lower(e.label) LIKE lower(:p) || ' %' ESCAPE '\\' DESC,"
        # a code such as B24 names the project, not a document whose title starts with it
        " CASE e.kind WHEN 'project' THEN 0 WHEN 'task' THEN 1 WHEN 'purchase_request' THEN 2 WHEN 'person' THEN 3"
        "  WHEN 'department' THEN 4 ELSE 5 END, length(e.label), e.id LIMIT 1"),
        {"s": s, "p": _like(s), **access.params()}).first()


def _live_statuses(conn, nodes) -> dict:
    live = {}
    for kind, table in LIVE_TABLES.items():
        ids = [str(n.ref_id) for n in nodes if n.kind == kind]
        if ids:
            live.update({(kind, r.id): r.status for r in conn.execute(
                text(f"SELECT id, status FROM {table} WHERE id = ANY(CAST(:ids AS uuid[]))"), {"ids": ids})})
    return live


def related(conn, principal, subject: str, depth: int = 2) -> RelatedResult | None:
    access = graph.graph_access(conn, principal)
    root = _resolve(conn, access, subject)
    if root is None:
        return None  # unresolvable or hidden: indistinguishable, and nothing is said about it
    previous = conn.execute(text("SELECT current_setting('statement_timeout')")).scalar()
    try:
        with conn.begin_nested():
            hood = graph.neighbourhood(conn, principal, root.kind, root.ref_id, depth)
    except NotFound:
        graph.log_read(principal, "assistant", depth, "not_found", root_kind=root.kind, root=root.ref_id)
        return None
    except graph.GraphTimeout as exc:
        graph.log_read(principal, "assistant", depth, "timeout", root_kind=root.kind, root=root.ref_id)
        raise GraphDegraded("GRAPH_TIMEOUT") from exc
    finally:
        conn.execute(text("SELECT set_config('statement_timeout', :v, true)"), {"v": previous})
    graph.log_read(principal, "assistant", depth, "ok", root_kind=root.kind, root=root.ref_id, nodes=len(hood.nodes))

    state = conn.execute(text("SELECT last_finished_at, last_status FROM core.graph_projection_state")).first()
    projection_stale = (state is None or state.last_status != "ok" or state.last_finished_at is None
                        or datetime.now(UTC) - state.last_finished_at > STALE_AFTER)
    nodes = {n.id: n for n in hood.nodes}
    live = _live_statuses(conn, hood.nodes)

    def status(n):
        return live.get((n.kind, n.ref_id), n.status)

    def changed(n):
        return (n.kind, n.ref_id) in live and live[(n.kind, n.ref_id)] != n.status

    ordered = sorted(hood.edges, key=lambda e: (e.kind not in CAUSAL_EDGE_KINDS,
                                                 hood.root_id not in (e.src_id, e.dst_id), e.kind, str(e.id)))
    facts, lines = [], []
    for e in ordered[:MAX_EDGES]:
        s, d = nodes[e.src_id], nodes[e.dst_id]
        fact = EdgeFact(s.label, s.kind, status(s), e.kind, d.label, d.kind, status(d), e.authority,
                        stale=projection_stale or changed(s) or changed(d), src_id=str(s.id), dst_id=str(d.id))
        quote = next((ev.quote for ev in e.evidence if ev.quote), None)
        line = (f"{s.label} [{s.kind}{', status=' + status(s) if status(s) else ''}] -{e.kind}-> "
                f"{d.label} [{d.kind}{', status=' + status(d) if status(d) else ''}] (authority={e.authority})")
        if quote:
            line += f'\n  source sentence: "{quote}"'
        if sum(len(x) + 1 for x in lines) + len(line) > MAX_CHARS:
            break
        facts.append(fact)
        lines.append(line)
    if not facts:
        return None
    return RelatedResult(root.label, "\n".join(lines), tuple(facts), projection_stale, hood.projected_at)
