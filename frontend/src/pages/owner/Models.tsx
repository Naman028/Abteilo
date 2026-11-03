import { FormEvent, useState } from "react";
import { api } from "../../api";
import { useAction, useLoad, useMe } from "../../hooks";

interface Profile { code: string; display_name: string; provider: string; model_name: string; purpose: string; max_output_tokens: number; temperature: string; enabled: boolean; external: boolean }
interface Route { department_id: string | null; department_name: string | null; task_class: "general" | "heavy"; profile_code: string }
interface ProviderStatus { provider: string; configured: boolean; reachable: boolean | null; installed_models: string[] }

export default function Models() {
  const { me } = useMe();
  const profiles = useLoad<Profile[]>("/api/admin/models");
  const routes = useLoad<Route[]>("/api/admin/model-routes");
  const providers = useLoad<ProviderStatus[]>("/api/admin/model-providers");
  const { busy, error, run } = useAction();
  const [tests, setTests] = useState<Record<string, string>>({});
  const [editing, setEditing] = useState<string | null>(null);

  const toggle = async (p: Profile) => {
    if (await run(() => api.patch(`/api/admin/models/${p.code}`, { enabled: !p.enabled }))) profiles.reload();
  };
  const test = async (p: Profile) => {
    setTests((t) => ({ ...t, [p.code]: "testing…" }));
    const r = await run(() => api.post<{ ok: boolean; error_code: string | null }>(`/api/admin/models/${p.code}/test`));
    setTests((t) => ({ ...t, [p.code]: r ? (r.ok ? "OK" : r.error_code ?? "failed") : "failed" }));
  };
  const setRoute = async (department_id: string | null, task_class: string, profile_code: string) => {
    if (await run(() => api.put("/api/admin/model-routes", { department_id, task_class, profile_code }))) routes.reload();
  };
  const routeFor = (dept: string | null, cls: string) =>
    routes.data?.find((r) => r.department_id === dept && r.task_class === cls)?.profile_code ?? "";

  return (
    <div className="stack">
      <div className="grid stats">
        {(providers.data ?? []).map((p) => (
          <div key={p.provider} className="card stat">
            <div className="label">{p.provider}</div>
            <div><span className={`badge ${p.configured && p.reachable !== false ? "ok" : "warn"}`}>
              {p.provider === "ollama" ? (p.reachable ? "running" : "not reachable") : p.configured ? "key configured" : "not configured"}
            </span></div>
            {p.installed_models.length > 0 && <div className="sub mono" style={{ marginTop: 6 }}>{p.installed_models.join(", ")}</div>}
          </div>
        ))}
      </div>

      <div className="card table-wrap">
        <h2>Model profiles</h2>
        {error && <div className="error">{error}</div>}
        <table>
          <thead><tr><th>Profile</th><th>Provider / model</th><th>Purpose</th><th>Status</th><th></th></tr></thead>
          <tbody>
            {(profiles.data ?? []).map((p) => (
              <tr key={p.code}>
                <td><strong>{p.display_name}</strong><div className="mono">{p.code}</div></td>
                <td><span className={`badge ${p.external ? "warn" : "ok"}`}>{p.external ? "external" : "local"}</span>{" "}
                  <span className="mono">{p.provider}: {p.model_name}</span></td>
                <td>{p.purpose}</td>
                <td><span className={`badge ${p.enabled ? "ok" : "bad"}`}>{p.enabled ? "enabled" : "disabled"}</span></td>
                <td>
                  <div className="row">
                    <button className="small" disabled={busy} onClick={() => toggle(p)}>{p.enabled ? "Disable" : "Enable"}</button>
                    <button className="small" disabled={busy} onClick={() => test(p)}>Test</button>
                    <button className="small" onClick={() => setEditing(editing === p.code ? null : p.code)}>Edit</button>
                    {tests[p.code] && <span className="mono">{tests[p.code]}</span>}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {editing && profiles.data && (
        <EditProfile key={editing} profile={profiles.data.find((p) => p.code === editing)!}
                     onDone={() => { setEditing(null); profiles.reload(); }} />
      )}

      <div className="card table-wrap">
        <h2>Routing</h2>
        <p className="sub">Each workspace sends general and heavy work to a model profile. Departments without their own route use the company default.</p>
        <table>
          <thead><tr><th>Workspace</th><th>General work</th><th>Heavy work</th></tr></thead>
          <tbody>
            {[{ id: null as string | null, name: "Company default" }, ...me.departments].map((d) => (
              <tr key={d.id ?? "default"}>
                <td>{d.name}</td>
                {(["general", "heavy"] as const).map((cls) => (
                  <td key={cls}>
                    <div className="row" style={{ flexWrap: "nowrap" }}>
                      <select value={routeFor(d.id, cls)} onChange={(e) => setRoute(d.id, cls, e.target.value)}>
                        {d.id !== null && <option value="" disabled>Company default ({routeFor(null, cls)})</option>}
                        {(profiles.data ?? []).map((p) => <option key={p.code} value={p.code}>{p.display_name}</option>)}
                      </select>
                      {d.id !== null && routeFor(d.id, cls) && (
                        <button className="small" title="Use the company default again" disabled={busy}
                                onClick={() => run(() => api.del(`/api/admin/model-routes?department_id=${d.id}&task_class=${cls}`)).then(routes.reload)}>
                          Reset
                        </button>
                      )}
                    </div>
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <NewProfile onCreated={profiles.reload} />
    </div>
  );
}

function EditProfile({ profile, onDone }: { profile: Profile; onDone: () => void }) {
  const [f, setF] = useState({ display_name: profile.display_name, model_name: profile.model_name, purpose: profile.purpose,
                               max_output_tokens: profile.max_output_tokens, temperature: Number(profile.temperature) });
  const { busy, error, run } = useAction();
  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (await run(() => api.patch(`/api/admin/models/${profile.code}`, f))) onDone();
  };
  return (
    <form className="card form" onSubmit={save}>
      <h2>Edit {profile.display_name} <span className="mono sub">({profile.provider})</span></h2>
      <div className="form inline">
        <label>Display name<input value={f.display_name} onChange={(e) => setF({ ...f, display_name: e.target.value })} required minLength={2} /></label>
        <label>Model name<input value={f.model_name} onChange={(e) => setF({ ...f, model_name: e.target.value })} required /></label>
        <label>Max output tokens<input type="number" min={64} max={16000} value={f.max_output_tokens}
                                       onChange={(e) => setF({ ...f, max_output_tokens: Number(e.target.value) })} /></label>
        <label>Temperature<input type="number" min={0} max={1} step={0.05} value={f.temperature}
                                 onChange={(e) => setF({ ...f, temperature: Number(e.target.value) })} /></label>
      </div>
      <label>Purpose<input value={f.purpose} onChange={(e) => setF({ ...f, purpose: e.target.value })} maxLength={300} /></label>
      <div className="row"><button className="primary" disabled={busy}>Save profile</button><button type="button" onClick={onDone}>Cancel</button></div>
      {error && <div className="error">{error}</div>}
    </form>
  );
}

function NewProfile({ onCreated }: { onCreated: () => void }) {
  const [f, setF] = useState({ code: "", display_name: "", provider: "ollama", model_name: "", purpose: "", max_output_tokens: 1500 });
  const { busy, error, run } = useAction();
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) =>
    setF({ ...f, [k]: k === "max_output_tokens" ? Number(e.target.value) : e.target.value });
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (await run(() => api.post("/api/admin/models", f))) {
      setF({ ...f, code: "", display_name: "", model_name: "", purpose: "" });
      onCreated();
    }
  };
  return (
    <form className="card form" onSubmit={submit}>
      <h2>Add a dedicated model</h2>
      <p className="sub">For example a specialist model for one department. Pull it in Ollama first (<span className="mono">ollama pull name</span>), then route work to it above.</p>
      <div className="form inline">
        <label>Code<input value={f.code} onChange={set("code")} placeholder="legal_local" pattern="[a-z][a-z0-9_]{1,40}" required /></label>
        <label>Display name<input value={f.display_name} onChange={set("display_name")} required minLength={2} /></label>
        <label>Provider
          <select value={f.provider} onChange={set("provider")}>
            <option value="ollama">Ollama (local, open source)</option>
            <option value="openai_compatible">OpenAI-compatible API</option>
            <option value="anthropic">Anthropic (Claude)</option>
          </select>
        </label>
        <label>Model name<input value={f.model_name} onChange={set("model_name")} placeholder="qwen2.5:14b" required /></label>
        <label>Max output tokens<input type="number" value={f.max_output_tokens} onChange={set("max_output_tokens")} min={64} max={16000} /></label>
      </div>
      <label>Purpose<input value={f.purpose} onChange={set("purpose")} /></label>
      <button className="primary" disabled={busy}>Add model profile</button>
      {error && <div className="error">{error}</div>}
    </form>
  );
}
