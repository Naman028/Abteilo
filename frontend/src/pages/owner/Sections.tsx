import { FormEvent, useEffect, useState } from "react";
import { api, day, when } from "../../api";
import { useAction, useLoad, useMe } from "../../hooks";
import type { GraphStatus } from "../../types";

// --- Usage ------------------------------------------------------------------------------
interface UsageRow { day: string; department_name: string | null; profile_code: string; runs: number; errors: number; input_tokens: number; output_tokens: number; avg_latency_ms: number }

export function Usage() {
  const [days, setDays] = useState(7);
  const usage = useLoad<UsageRow[]>(`/api/admin/usage?days=${days}`);
  const rows = usage.data ?? [];
  const total = (k: "runs" | "errors" | "input_tokens" | "output_tokens") => rows.reduce((s, r) => s + r[k], 0);
  return (
    <div className="stack">
      <div className="row">
        <span className="sub spacer">Aggregated per department and model. Usage is never reported per person.</span>
        <select value={days} onChange={(e) => setDays(Number(e.target.value))} style={{ width: "auto" }}>
          {[1, 7, 30, 90].map((d) => <option key={d} value={d}>Last {d} days</option>)}
        </select>
      </div>
      <div className="grid stats">
        <div className="card stat"><div className="value">{total("runs")}</div><div className="label">Model calls</div></div>
        <div className="card stat"><div className="value">{total("errors")}</div><div className="label">Failed calls</div></div>
        <div className="card stat"><div className="value">{total("input_tokens").toLocaleString("de-DE")}</div><div className="label">Input tokens</div></div>
        <div className="card stat"><div className="value">{total("output_tokens").toLocaleString("de-DE")}</div><div className="label">Output tokens</div></div>
      </div>
      <div className="card table-wrap">
        <table>
          <thead><tr><th>Day</th><th>Workspace</th><th>Model profile</th><th className="num">Calls</th><th className="num">Errors</th><th className="num">Tokens in / out</th><th className="num">Avg latency</th></tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}><td>{day(r.day)}</td><td>{r.department_name ?? "Owner / admin"}</td><td className="mono">{r.profile_code}</td>
                <td className="num">{r.runs}</td><td className="num">{r.errors}</td>
                <td className="num">{r.input_tokens} / {r.output_tokens}</td><td className="num">{(r.avg_latency_ms / 1000).toFixed(1)} s</td></tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && <div className="empty">No AI usage in this period.</div>}
      </div>
    </div>
  );
}

// --- Data & retention ------------------------------------------------------------------------
interface Inventory {
  departments: { department_id: string; department_name: string; status: string; documents: number; projects: number; tasks: number;
                 purchase_requests: number; index_pending: number; index_indexed: number; index_failed: number }[];
  embedding_model: string | null; index_errors: Record<string, number>;
  leave_requests: number; access_requests: number; audit_events: number; ai_runs: number;
  conversations_note: string; chat_retention_days: number | null; last_purge_at: string | null;
}

export function DataRetention() {
  const data = useLoad<Inventory>("/api/admin/data");
  const { busy, error, run } = useAction();
  const [result, setResult] = useState<string | null>(null);
  const purge = async () => {
    const r = await run(() => api.post<{ conversations_deleted: number; runs_pseudonymised: number; retention_days: number | null }>("/api/admin/retention/run"));
    if (r) {
      setResult(r.retention_days === null ? "Retention is set to keep chats; nothing was deleted."
        : `Deleted ${r.conversations_deleted} expired chats, pseudonymised ${r.runs_pseudonymised} usage records.`);
      data.reload();
    }
  };
  const d = data.data;
  return (
    <div className="stack">
      <div className="notice">
        Company business data is protected: the application's database role has no permission to delete projects, tasks,
        budgets, requests, documents, departments or audit history. Only chats are subject to deletion.
      </div>
      {d && (
        <>
          <div className="card table-wrap">
            <h2>Company dataset</h2>
            <table>
              <thead><tr><th>Department</th><th>Status</th><th className="num">Documents</th><th className="num">Projects</th><th className="num">Tasks</th><th className="num">Purchase requests</th><th>Search index</th></tr></thead>
              <tbody>
                {d.departments.map((x) => (
                  <tr key={x.department_id}><td>{x.department_name}</td><td><span className={`badge ${x.status}`}>{x.status}</span></td>
                    <td className="num">{x.documents}</td><td className="num">{x.projects}</td><td className="num">{x.tasks}</td><td className="num">{x.purchase_requests}</td>
                    <td><span className="sub">{x.index_indexed} indexed</span>{x.index_pending > 0 && <> · <span className="badge warn">{x.index_pending} pending</span></>}
                      {x.index_failed > 0 && <> · <span className="badge bad">{x.index_failed} failed</span></>}</td></tr>
                ))}
              </tbody>
            </table>
            <p className="sub" style={{ marginTop: 8 }}>
              Leave requests: {d.leave_requests} · Access requests: {d.access_requests} · Audit events: {d.audit_events} · AI usage records: {d.ai_runs}
            </p>
          </div>
          <div className="card">
            <h2>Semantic search index</h2>
            <p>Embedding model: <strong>{d.embedding_model ?? "off (keyword search only)"}</strong>. New and changed documents are
              indexed by the background worker within about a minute. Access rules are checked at search time, so index state never
              widens access.</p>
            {Object.keys(d.index_errors).length > 0 && (
              <p className="sub">Errors: {Object.entries(d.index_errors).map(([k, v]) => `${k} (${v})`).join(", ")}</p>
            )}
            <button disabled={busy} onClick={async () => {
              const r = await run(() => api.post<{ requeued: number }>("/api/admin/index/retry"));
              if (r) { setResult(`Re-queued ${r.requeued} document versions for indexing.`); data.reload(); }
            }}>Retry failed indexing</button>
          </div>
          <KnowledgeGraph />
          <div className="card">
            <h2>Chat retention</h2>
            <p>Current policy: <strong>{d.chat_retention_days ? `delete after ${d.chat_retention_days} days` : "keep until the user deletes them"}</strong>
              {" "}(change it under Settings). Last automatic purge: {d.last_purge_at ? when(d.last_purge_at) : "never"}.</p>
            <p className="sub">{d.conversations_note}</p>
            <button disabled={busy} onClick={purge}>Apply retention now</button>
            {result && <div className="notice" style={{ marginTop: 10 }}>{result}</div>}
            {error && <div className="error">{error}</div>}
          </div>
        </>
      )}
    </div>
  );
}

const AUTHORITY_LABEL: Record<string, string> = { record: "from records", document_mention: "document mentions", reviewed: "reviewed" };

/** Counts and health only: the owner console never shows the records behind the graph. */
function KnowledgeGraph() {
  const status = useLoad<GraphStatus>("/api/admin/graph");
  const { busy, error, run } = useAction();
  const [result, setResult] = useState<string | null>(null);
  const s = status.data;
  if (!s) return null;
  const count = (m: Record<string, number>) => Object.values(m).reduce((a, b) => a + b, 0);
  return (
    <div className="card">
      <h2>Knowledge graph</h2>
      <p>
        Last update: <strong>{s.last_finished_at ? when(s.last_finished_at) : "not built yet"}</strong>
        {s.last_status && <> · <span className={`badge ${s.last_status === "ok" ? "ok" : "bad"}`}>{s.last_status}</span></>}
        {s.last_error_code && <span className="sub"> {s.last_error_code}</span>}
      </p>
      <p className="sub">
        {count(s.entities)} records ({Object.entries(s.entities).map(([k, v]) => `${v} ${k.replace("_", " ")}`).join(", ") || "none"}) ·{" "}
        {count(s.relationships)} current relationships ({Object.entries(s.relationships).map(([k, v]) => `${v} ${AUTHORITY_LABEL[k] ?? k}`).join(", ") || "none"}) ·{" "}
        {s.ended_relationships} ended, kept as history
      </p>
      <p className="sub">
        Suggestions from documents: {Object.entries(s.candidates).map(([k, v]) => `${v} ${k}`).join(", ") || "none"}.
        {s.extraction_failed > 0 && ` Extraction failed for ${s.extraction_failed} document versions (${Object.entries(s.extraction_errors).map(([k, v]) => `${k} ${v}`).join(", ")}).`}
        {" "}Department editors review suggestions in their Documents tab; nothing suggested is used before that.
      </p>
      <button disabled={busy} onClick={async () => {
        const r = await run(() => api.post<{ entities_changed: number; edges_added: number; edges_ended: number }>("/api/admin/graph/rebuild"));
        if (r) { setResult(`Updated ${r.entities_changed} records, added ${r.edges_added} and ended ${r.edges_ended} relationships.`); status.reload(); }
      }}>Update now</button>
      {result && <div className="notice" style={{ marginTop: 10 }}>{result}</div>}
      {error && <div className="error">{error}</div>}
    </div>
  );
}

// --- Audit ------------------------------------------------------------------------------
interface AuditRow { id: number; occurred_at: string; actor_name: string | null; action: string; resource_type: string; resource_id: string | null; details: Record<string, unknown> }

export function AuditLog() {
  const [prefix, setPrefix] = useState("");
  const audit = useLoad<AuditRow[]>(`/api/admin/audit?limit=200${prefix ? `&action_prefix=${prefix}` : ""}`);
  return (
    <div className="card">
      <div className="row" style={{ marginBottom: 10 }}>
        <h2 className="spacer">Audit log</h2>
        <a className="btn" href="/api/admin/audit.csv">Export CSV</a>
        <select value={prefix} onChange={(e) => setPrefix(e.target.value)} style={{ width: "auto" }}>
          <option value="">All events</option>
          {["auth", "company", "department", "member", "model", "engineering", "finance", "hr", "it", "documents", "retention", "privacy"].map((p) => (
            <option key={p} value={p}>{p}</option>
          ))}
        </select>
      </div>
      <div className="table-wrap">
        <table>
          <thead><tr><th>When</th><th>Who</th><th>Action</th><th>Resource</th><th>Details</th></tr></thead>
          <tbody>
            {(audit.data ?? []).map((a) => (
              <tr key={a.id}><td>{when(a.occurred_at)}</td><td>{a.actor_name ?? "system"}</td><td className="mono">{a.action}</td>
                <td>{a.resource_type}</td><td className="mono" style={{ fontSize: "0.75rem" }}>{Object.keys(a.details).length ? JSON.stringify(a.details) : ""}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// --- Settings ----------------------------------------------------------------------------
interface SettingsView { company_name: string; timezone: string; chat_retention_days: 30 | 60 | null; external_ai_enabled: boolean; external_ai_confirmed_at: string | null }

export function Settings() {
  const settings = useLoad<SettingsView>("/api/admin/settings");
  const [retention, setRetention] = useState<string>("keep");
  const [external, setExternal] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [saved, setSaved] = useState(false);
  const { busy, error, run } = useAction();

  useEffect(() => {
    if (settings.data) {
      setRetention(settings.data.chat_retention_days ? String(settings.data.chat_retention_days) : "keep");
      setExternal(settings.data.external_ai_enabled);
    }
  }, [settings.data]);

  const save = async (e: FormEvent) => {
    e.preventDefault();
    setSaved(false);
    const body = { chat_retention_days: retention === "keep" ? null : Number(retention), external_ai_enabled: external,
                   confirm_processing_agreement: confirm };
    if (await run(() => api.put("/api/admin/settings", body))) { setSaved(true); settings.reload(); }
  };

  return (
    <div className="grid two">
    {settings.data && <CompanyProfile name={settings.data.company_name} timezone={settings.data.timezone} onSaved={settings.reload} />}
    <form className="card form" onSubmit={save}>
      <h2>Data protection settings</h2>
      <label>Chat retention
        <select value={retention} onChange={(e) => setRetention(e.target.value)}>
          <option value="keep">Keep chats until the user deletes them</option>
          <option value="30">Delete chats 30 days after the last message</option>
          <option value="60">Delete chats 60 days after the last message</option>
        </select>
      </label>
      <label style={{ flexDirection: "row", alignItems: "center", gap: 8, color: "var(--text)" }}>
        <input type="checkbox" checked={external} onChange={(e) => setExternal(e.target.checked)} style={{ width: "auto" }} />
        Allow external AI providers (hosted models such as Claude) for heavy tasks
      </label>
      {external && !settings.data?.external_ai_enabled && (
        <label style={{ flexDirection: "row", alignItems: "flex-start", gap: 8, color: "var(--text)" }}>
          <input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} style={{ width: "auto", marginTop: 4 }} />
          I confirm a data processing agreement (Art. 28 GDPR) and, where data leaves the EU/EEA, a transfer assessment
          exist for the configured provider, and employees have been informed.
        </label>
      )}
      {settings.data?.external_ai_confirmed_at && <p className="sub">External AI confirmed on {day(settings.data.external_ai_confirmed_at)}.</p>}
      <p className="sub">Local open-source models run on your own hardware and are always allowed. Business records are never deleted by these settings.</p>
      <button className="primary" disabled={busy}>Save settings</button>
      {saved && <div className="notice">Saved.</div>}
      {error && <div className="error">{error}</div>}
    </form>
    </div>
  );
}

function CompanyProfile({ name, timezone, onSaved }: { name: string; timezone: string; onSaved: () => void }) {
  const { refresh } = useMe();
  const [n, setN] = useState(name);
  const [tz, setTz] = useState(timezone);
  const { busy, error, run } = useAction();
  const [saved, setSaved] = useState(false);
  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (await run(() => api.put("/api/admin/company", { name: n, timezone: tz }))) { setSaved(true); onSaved(); refresh(); }
  };
  return (
    <form className="card form" onSubmit={save}>
      <h2>Company profile</h2>
      <label>Company name<input value={n} onChange={(e) => setN(e.target.value)} required minLength={2} maxLength={160} /></label>
      <label>Time zone<input value={tz} onChange={(e) => setTz(e.target.value)} placeholder="Europe/Berlin" required /></label>
      <button className="primary" disabled={busy}>Save profile</button>
      {saved && <div className="notice">Saved.</div>}
      {error && <div className="error">{error}</div>}
    </form>
  );
}
