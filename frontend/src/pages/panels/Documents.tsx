import { FormEvent, useState } from "react";
import { api, day } from "../../api";
import { Suggestions } from "../../components/Connections";
import { useAction, useLoad } from "../../hooks";
import type { DocumentContent, DocumentView, SearchResult, Workspace } from "../../types";

const MATCH_LABEL: Record<string, string> = { keyword: "keyword", semantic: "semantic", both: "keyword + semantic" };

export default function Documents({ ws }: { ws: Workspace }) {
  const docs = useLoad<DocumentView[]>(`/api/departments/${ws.department_id}/documents`);
  const [open, setOpen] = useState<DocumentContent | null>(null);
  const { busy, error, run } = useAction();
  const canWrite = ws.permissions.includes("documents.write");

  const view = async (id: string) => {
    const doc = await run(() => api.get<DocumentContent>(`/api/documents/${id}`));
    if (doc) setOpen(doc);
  };
  const archive = async (id: string) => {
    if (await run(() => api.post(`/api/documents/${id}/archive`))) {
      setOpen(null);
      docs.reload();
    }
  };

  return (
    <div className="stack">
    <Search departmentId={ws.department_id} onOpen={view} />
    {canWrite && <Suggestions departmentId={ws.department_id} />}
    <div className="grid two">
      <div className="card">
        <h2>Documents</h2>
        <table>
          <thead><tr><th>Title</th><th>Visibility</th><th>Updated</th></tr></thead>
          <tbody>
            {(docs.data ?? []).map((d) => (
              <tr key={d.id} className={`selectable${open?.id === d.id ? " selected" : ""}`} onClick={() => view(d.id)}>
                <td>
                  <button type="button" className="row-select" onClick={(e) => { e.stopPropagation(); view(d.id); }}>{d.title}</button>{" "}
                  <span className="sub">v{d.current_version}</span>
                </td>
                <td><span className={`badge ${d.classification}`}>{d.classification}</span></td>
                <td>{day(d.updated_at)} <IndexBadge status={d.index_status} /></td>
              </tr>
            ))}
          </tbody>
        </table>
        {docs.data?.length === 0 && <div className="empty">No documents you can read here.</div>}
        {canWrite && <Upload departmentId={ws.department_id} onUploaded={docs.reload} />}
        {error && <div className="error">{error}</div>}
      </div>
      <div className="card">
        {open ? (
          <>
            <div className="row">
              <h2 className="spacer">{open.title}</h2>
              {canWrite && <button className="small danger" disabled={busy} onClick={() => archive(open.id)}>Archive</button>}
            </div>
            <div className="sub mono" style={{ marginBottom: 8 }}>v{open.current_version} · sha256 {open.content_sha256.slice(0, 12)}… · <IndexBadge status={open.index_status} /></div>
            <div className="doc-view">{open.content}</div>
            {canWrite && <NewVersion documentId={open.id} onUploaded={async () => { docs.reload(); await view(open.id); }} />}
          </>
        ) : <div className="empty">Select a document to read it. Archived documents are kept, never deleted.</div>}
      </div>
    </div>
    </div>
  );
}

function IndexBadge({ status }: { status: string }) {
  const label = status === "indexed" ? "searchable" : status === "pending" ? "indexing…" : "index failed";
  return <span className={`badge ${status === "indexed" ? "ok" : status === "pending" ? "warn" : "bad"}`}
               title="Semantic search index status of the current version">{label}</span>;
}

function Search({ departmentId, onOpen }: { departmentId: string; onOpen: (id: string) => void }) {
  const [q, setQ] = useState("");
  const [mode, setMode] = useState("hybrid");
  const [result, setResult] = useState<SearchResult | null>(null);
  const { busy, error, run } = useAction();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const params = new URLSearchParams({ q, mode, department_id: departmentId });
    const r = await run(() => api.get<SearchResult>(`/api/documents-search?${params}`));
    if (r) setResult(r);
  };
  return (
    <div className="card">
      <form className="row" onSubmit={submit}>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search documents you can read (any wording, English or German)"
               minLength={2} maxLength={300} required style={{ flex: 1, minWidth: 220 }} />
        <select value={mode} onChange={(e) => setMode(e.target.value)} style={{ width: "auto" }}>
          <option value="hybrid">Hybrid</option>
          <option value="keyword">Keyword only</option>
          <option value="vector">Semantic only</option>
        </select>
        <button className="primary" disabled={busy}>Search</button>
      </form>
      {error && <div className="error">{error}</div>}
      {result?.degraded && (
        <div className="notice" style={{ marginTop: 8 }}>
          Semantic search is unavailable ({result.degraded}); showing keyword results only. Access rules are unchanged.
        </div>
      )}
      {result && (
        <div className="stack" style={{ gap: 8, marginTop: 10 }}>
          {result.hits.length === 0 && <div className="empty">No matching passages you are allowed to read.</div>}
          {result.hits.map((h) => (
            <div key={h.chunk_id} style={{ borderTop: "1px solid var(--border)", paddingTop: 8 }}>
              <div className="row">
                <a className="spacer" href="#" onClick={(e) => { e.preventDefault(); onOpen(h.document_id); }}>
                  <strong>{h.document_title}</strong>
                </a>
                <span className="sub">{h.department_name} · v{h.version}</span>
                <span className={`badge ${h.match === "both" ? "ok" : ""}`}
                      title={`keyword rank ${h.keyword_rank ?? "–"} · semantic rank ${h.vector_rank ?? "–"}${h.distance !== null ? ` · distance ${h.distance}` : ""}`}>
                  {MATCH_LABEL[h.match]}
                </span>
              </div>
              <div className="sub" style={{ whiteSpace: "pre-wrap" }}>{h.content.slice(0, 280)}{h.content.length > 280 ? "…" : ""}</div>
            </div>
          ))}
          <div className="sub" style={{ fontSize: "0.78rem" }}>
            Mode: {result.mode}{result.embedding_model ? ` · embeddings: ${result.embedding_model}` : ""} · ranked by Reciprocal Rank Fusion
          </div>
        </div>
      )}
    </div>
  );
}

function NewVersion({ documentId, onUploaded }: { documentId: string; onUploaded: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const { busy, error, run } = useAction();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    if (await run(() => api.upload(`/api/documents/${documentId}/versions`, form))) {
      (e.target as HTMLFormElement).reset();
      onUploaded();
    }
  };
  return (
    <form className="row" onSubmit={submit} style={{ marginTop: 10 }}>
      <input type="file" accept=".txt,.md,text/plain,text/markdown" onChange={(e) => setFile(e.target.files?.[0] ?? null)} required
             style={{ flex: 1 }} />
      <button disabled={busy}>Upload new version</button>
      {error && <div className="error">{error}</div>}
    </form>
  );
}

function Upload({ departmentId, onUploaded }: { departmentId: string; onUploaded: () => void }) {
  const [title, setTitle] = useState("");
  const [classification, setClassification] = useState("department");
  const [file, setFile] = useState<File | null>(null);
  const { busy, error, run } = useAction();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!file) return;
    const form = new FormData();
    form.append("title", title);
    form.append("classification", classification);
    form.append("file", file);
    if (await run(() => api.upload(`/api/departments/${departmentId}/documents`, form))) {
      setTitle("");
      setFile(null);
      (e.target as HTMLFormElement).reset();
      onUploaded();
    }
  };
  return (
    <form className="form" onSubmit={submit} style={{ marginTop: 14 }}>
      <h3>Upload text or Markdown (UTF-8, max 1 MiB)</h3>
      <label>Title<input value={title} onChange={(e) => setTitle(e.target.value)} required minLength={2} /></label>
      <label>Visibility
        <select value={classification} onChange={(e) => setClassification(e.target.value)}>
          <option value="company">Company – every employee</option>
          <option value="department">Department members</option>
          <option value="confidential">Confidential – department leads</option>
        </select>
      </label>
      <input type="file" accept=".txt,.md,text/plain,text/markdown" onChange={(e) => setFile(e.target.files?.[0] ?? null)} required />
      <button className="primary" disabled={busy}>Upload</button>
      {error && <div className="error">{error}</div>}
    </form>
  );
}
