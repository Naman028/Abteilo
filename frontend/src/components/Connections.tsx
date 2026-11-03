import { useState } from "react";
import { api, day, when } from "../api";
import { useAction, useLoad } from "../hooks";
import type { GraphCandidate, GraphEdge, GraphKind, GraphNode, Neighbourhood } from "../types";

export const KIND_LABEL: Record<GraphKind, string> = {
  department: "Department", project: "Project", task: "Task", person: "Person", purchase_request: "Purchase request",
  leave_request: "Leave request", access_request: "Access request", document: "Document",
};

const EDGE_LABEL: Record<string, string> = {
  owns: "owns", has_task: "has task", assigned_to: "assigned to", member_of: "member of", requested_in: "requested in",
  requested_for: "requested for", requested_by: "requested by", decided_by: "decided by", targets: "for",
  proposed_by: "proposed by", for_department: "in", mentions: "mentions", depends_on: "depends on",
  blocked_by: "blocked by", delays: "delays", affects: "affects",
};

const AUTHORITY: Record<GraphEdge["authority"], { label: string; css: string; title: string }> = {
  record: { label: "record", css: "verified", title: "Taken from a company record" },
  document_mention: { label: "mention", css: "cited", title: "The current document version names this project" },
  reviewed: { label: "reviewed", css: "cited", title: "Stated in a document and accepted by a reviewer" },
};

/** "Connected to": the visible relationships around one record. Hidden records are simply absent. */
export default function Connections({ kind, refId }: { kind: GraphKind; refId: string }) {
  const [depth, setDepth] = useState(1);
  const hood = useLoad<Neighbourhood>(`/api/graph/${kind}/${refId}/neighbourhood?depth=${depth}`);
  const data = hood.data;
  if (hood.error) return <p className="sub">Not in the relationship graph yet (it updates within a minute).</p>;
  if (!data) return null;
  const nodes = new Map<string, GraphNode>(data.nodes.map((n) => [n.id, n]));
  const edges = [...data.edges].sort((a, b) =>
    (nodes.get(a.src_id)!.depth + nodes.get(a.dst_id)!.depth) - (nodes.get(b.src_id)!.depth + nodes.get(b.dst_id)!.depth));
  return (
    <div className="connections">
      <div className="row">
        <h3 className="spacer">Connected to</h3>
        <select value={depth} onChange={(e) => setDepth(Number(e.target.value))} style={{ width: "auto" }} aria-label="Depth">
          <option value={1}>direct</option><option value={2}>2 steps</option><option value={3}>3 steps</option>
        </select>
      </div>
      {edges.length === 0 && <p className="sub">No relationships you can see.</p>}
      <ul className="edge-list">
        {edges.map((e) => <EdgeRow key={e.id} edge={e} nodes={nodes} rootId={data.root_id} />)}
      </ul>
      <p className="sub">
        Only records you may open are shown; people and departments are endpoints, never stepping stones.
        {data.truncated && " The list is cut at its size limit."}
        {data.projected_at && ` Graph updated ${when(data.projected_at)}.`}
      </p>
    </div>
  );
}

function EdgeRow({ edge, nodes, rootId }: { edge: GraphEdge; nodes: Map<string, GraphNode>; rootId: string }) {
  const src = nodes.get(edge.src_id)!;
  const dst = nodes.get(edge.dst_id)!;
  const a = AUTHORITY[edge.authority];
  const quote = edge.evidence.find((x) => x.quote)?.quote;
  return (
    <li>
      <NodeLabel node={src} root={src.id === rootId} />
      <span className="edge-kind">{EDGE_LABEL[edge.kind] ?? edge.kind}</span>
      <NodeLabel node={dst} root={dst.id === rootId} />
      <span className={`badge ${a.css}`} title={a.title}>{a.label}</span>
      {quote && <div className="quote">“{quote}”</div>}
    </li>
  );
}

function NodeLabel({ node, root }: { node: GraphNode; root: boolean }) {
  return (
    <span className={root ? "node root" : "node"} title={KIND_LABEL[node.kind]}>
      <span className="node-kind">{KIND_LABEL[node.kind]}</span> {node.label}
      {node.status && !root && <span className={`badge ${node.status}`}>{node.status}</span>}
    </span>
  );
}

const CANDIDATE_LABEL: Record<GraphCandidate["kind"], string> = {
  depends_on: "depends on", blocked_by: "is blocked by", delays: "delays", affects: "affects",
};

/** Department editors confirm or reject relationships a model suggested from their documents. */
export function Suggestions({ departmentId }: { departmentId: string }) {
  const list = useLoad<GraphCandidate[]>(`/api/departments/${departmentId}/graph/candidates`);
  const { busy, error, run } = useAction();
  const decide = async (id: string, decision: "accept" | "reject") => {
    await run(() => api.post(`/api/graph/candidates/${id}/decision`, { decision }));
    list.reload();
  };
  if (list.error || !list.data || list.data.length === 0) return null;
  return (
    <div className="card">
      <h2>Suggested relationships</h2>
      <p className="sub">
        The local model read these documents and suggested links between records. A suggestion is not used anywhere until
        you accept it; check the quoted sentence first.
      </p>
      <ul className="edge-list">
        {list.data.map((c) => (
          <li key={c.id}>
            <span className="node"><span className="node-kind">{KIND_LABEL[c.source.kind]}</span> {c.source.label}</span>
            <span className="edge-kind">{CANDIDATE_LABEL[c.kind]}</span>
            <span className="node"><span className="node-kind">{KIND_LABEL[c.target.kind]}</span> {c.target.label}</span>
            <div className="quote">“{c.quote}” <span className="sub">{c.document_title} v{c.version} · {day(c.created_at)}</span></div>
            <div className="row" style={{ marginTop: 6 }}>
              <button className="small primary" disabled={busy} onClick={() => decide(c.id, "accept")}>Accept</button>
              <button className="small" disabled={busy} onClick={() => decide(c.id, "reject")}>Reject</button>
            </div>
          </li>
        ))}
      </ul>
      {error && <div className="error">{error}</div>}
    </div>
  );
}
