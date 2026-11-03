import { api, euro, when } from "../../api";
import { useAction, useLoad } from "../../hooks";
import { DecisionButtons, Stat, type Decision } from "../../components/ui";
import type { AccessRequest } from "../../types";

interface Overview {
  company_name: string;
  generated_at: string;
  active_members: number;
  active_departments: number;
  period: string;
  pending: { purchase_requests: number; purchase_amount_cents: number; leave_requests: number; access_requests: number; elevated_access_requests: number };
  blocked_projects: { code: string; name: string; department_name: string; blocked_tasks: string[] }[];
  budget_pressure: { department_name: string; period: string; budget_cents: number; approved_cents: number; pending_cents: number }[];
  departments: { department_id: string; name: string; members: number; active_projects: number; blocked_projects: number; pending_purchases: number; documents: number }[];
  ai: { calls_7d: number; errors_7d: number; input_tokens_7d: number; output_tokens_7d: number; enabled_models: number; external_ai_enabled: boolean; ollama_reachable: boolean };
  activity_24h: { action: string; count: number }[];
  elevated_requests: AccessRequest[];
}

export default function OverviewTab({ go }: { go: (tab: string) => void }) {
  const overview = useLoad<Overview>("/api/admin/overview");
  const { busy, error, run } = useAction();
  const o = overview.data;
  if (overview.error) return <div className="error">{overview.error}</div>;
  if (!o) return <div className="sub">Loading…</div>;

  const decide = async (id: string, decision: Decision) => {
    if (await run(() => api.post(`/api/it/access-requests/${id}/decision`, { decision }))) overview.reload();
  };
  const p = o.pending;

  return (
    <div className="stack">
      <div className="card">
        <div className="section-head">
          <h2>Needs your decision</h2>
          <span className={`badge ${o.elevated_requests.length ? "warn" : "ok"}`}>{o.elevated_requests.length} waiting</span>
        </div>
        {o.elevated_requests.length === 0 ? (
          <p className="empty">Nothing waits for you. Elevated role changes that only an owner may approve appear here.</p>
        ) : (<>
          <p className="sub">These role changes grant company-wide control. Only an owner can approve them.</p>
          {error && <div className="error" role="alert">{error}</div>}
          <div className="table-wrap">
          <table>
            <thead><tr><th>Person</th><th>Role</th><th>Reason</th><th>Proposed by</th><th><span className="sr-only">Decision</span></th></tr></thead>
            <tbody>
              {o.elevated_requests.map((r) => (
                <tr key={r.id}>
                  <td>{r.target_name}</td><td><span className="badge bad">{r.role_code}</span></td><td>{r.justification}</td>
                  <td>{r.proposer_name}</td>
                  <td><DecisionButtons busy={busy} onDecide={(d) => decide(r.id, d)} /></td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        </>)}
      </div>

      <div className="grid stats">
        <Stat value={o.active_members} label="Active people" onClick={() => go("members")} />
        <Stat value={o.active_departments} label="Departments" onClick={() => go("departments")} />
        <Stat value={p.purchase_requests} label={`Purchases waiting · ${euro(p.purchase_amount_cents)}`} />
        <Stat value={p.leave_requests} label="Leave requests waiting" />
        <Stat value={p.access_requests + p.elevated_access_requests} label="Role changes waiting" />
        <Stat value={o.ai.calls_7d} label={`AI calls (7 days) · ${o.ai.errors_7d} failed`} onClick={() => go("usage")} />
      </div>

      <div className="grid two">
        <div className="card">
          <h2>Blocked work</h2>
          {o.blocked_projects.length === 0 && <div className="empty">Nothing is blocked.</div>}
          {o.blocked_projects.map((b) => (
            <div key={b.code} style={{ padding: "6px 0", borderBottom: "1px solid var(--border)" }}>
              <div className="row"><span className="mono">{b.code}</span><strong className="spacer">{b.name}</strong>
                <span className="sub">{b.department_name}</span></div>
              {b.blocked_tasks.length > 0 && <div className="sub">Blocked: {b.blocked_tasks.join(", ")}</div>}
            </div>
          ))}
        </div>
        <div className="card">
          <h2>Budget pressure · {o.period}</h2>
          <p className="sub">Departments whose approved and pending spending has reached 80% of the budget.</p>
          {o.budget_pressure.length === 0 && <div className="empty">No department is close to its budget.</div>}
          {o.budget_pressure.map((b) => (
            <div key={b.department_name} className="row" style={{ padding: "6px 0" }}>
              <span className="spacer">{b.department_name}</span>
              <span className="mono">{euro(b.approved_cents + b.pending_cents)} / {euro(b.budget_cents)}</span>
            </div>
          ))}
        </div>
      </div>

      <div className="card table-wrap">
        <h2>Departments</h2>
        <table>
          <thead><tr><th>Department</th><th className="num">People</th><th className="num">Active projects</th><th className="num">Blocked</th><th className="num">Purchases waiting</th><th className="num">Documents</th></tr></thead>
          <tbody>
            {o.departments.map((d) => (
              <tr key={d.department_id}><td>{d.name}</td><td className="num">{d.members}</td><td className="num">{d.active_projects}</td>
                <td className="num">{d.blocked_projects}</td><td className="num">{d.pending_purchases}</td><td className="num">{d.documents}</td></tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="grid two">
        <div className="card">
          <h2>AI platform</h2>
          <div className="row" style={{ marginBottom: 6 }}>
            <span className={`badge ${o.ai.ollama_reachable ? "ok" : "bad"}`}>Local models {o.ai.ollama_reachable ? "running" : "unreachable"}</span>
            <span className={`badge ${o.ai.external_ai_enabled ? "warn" : "ok"}`}>External AI {o.ai.external_ai_enabled ? "enabled" : "off"}</span>
          </div>
          <p className="sub">{o.ai.enabled_models} model profiles enabled · {o.ai.input_tokens_7d.toLocaleString("de-DE")} input /{" "}
            {o.ai.output_tokens_7d.toLocaleString("de-DE")} output tokens in 7 days</p>
          <button className="small" onClick={() => go("models")}>Manage models</button>
        </div>
        <div className="card">
          <h2>Activity · last 24 hours</h2>
          {o.activity_24h.length === 0 && <div className="empty">No business activity.</div>}
          {o.activity_24h.map((a) => (
            <div key={a.action} className="row"><span className="mono spacer">{a.action}</span><span>{a.count}</span></div>
          ))}
          <p className="sub" style={{ marginTop: 8 }}>Computed {when(o.generated_at)} directly from company records.</p>
        </div>
      </div>
    </div>
  );
}

