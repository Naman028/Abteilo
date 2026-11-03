import { FormEvent, useState } from "react";
import { api } from "../../api";
import { useAction, useLoad, useMe } from "../../hooks";

interface DeptAdmin { id: string; code: string; name: string; template: string; status: string; modules: string[]; members: number }
interface Template { code: string; name: string; modules: string[]; tools: string[] }
interface DeptMember { user_id: string; display_name: string; title: string | null; roles: string[] }

export function Departments() {
  const { refresh } = useMe();
  const depts = useLoad<DeptAdmin[]>("/api/admin/departments");
  const templates = useLoad<Template[]>("/api/admin/department-templates");
  const [openId, setOpenId] = useState<string | null>(null);
  const changed = () => { depts.reload(); refresh(); };
  const open = depts.data?.find((d) => d.id === openId) ?? null;

  return (
    <div className="stack">
      <div className="card table-wrap">
        <h2>Departments</h2>
        <table>
          <thead><tr><th>Name</th><th>Template</th><th>Modules</th><th className="num">People</th><th>Status</th><th></th></tr></thead>
          <tbody>
            {(depts.data ?? []).map((d) => (
              <tr key={d.id} style={d.id === openId ? { background: "var(--surface-2)" } : undefined}>
                <td><strong>{d.name}</strong><div className="mono">{d.code}</div></td><td>{d.template}</td>
                <td>{d.modules.join(", ")}</td><td className="num">{d.members}</td>
                <td><span className={`badge ${d.status}`}>{d.status}</span></td>
                <td><button className="small" onClick={() => setOpenId(d.id === openId ? null : d.id)}>{d.id === openId ? "Close" : "Manage"}</button></td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="sub" style={{ marginTop: 8 }}>Archiving hides a workspace. Its projects, requests, documents and history stay in the company dataset.</p>
      </div>
      {open && templates.data && (
        <DepartmentDetail key={open.id} dept={open} template={templates.data.find((t) => t.code === open.template)!} onChanged={changed} />
      )}
      <NewDepartment templates={templates.data ?? []} used={new Set((depts.data ?? []).map((d) => d.code))} onCreated={changed} />
    </div>
  );
}

function DepartmentDetail({ dept, template, onChanged }: { dept: DeptAdmin; template: Template; onChanged: () => void }) {
  const members = useLoad<DeptMember[]>(`/api/admin/departments/${dept.id}/members`);
  const [name, setName] = useState(dept.name);
  const [modules, setModules] = useState(new Set(dept.modules));
  const { busy, error, run } = useAction();
  const [saved, setSaved] = useState<string | null>(null);

  const save = async (e: FormEvent) => {
    e.preventDefault();
    setSaved(null);
    const ok = await run(async () => {
      if (name !== dept.name) await api.patch(`/api/admin/departments/${dept.id}`, { name });
      return api.put(`/api/admin/departments/${dept.id}/modules`, { modules: [...modules] });
    });
    if (ok) { setSaved("Saved."); onChanged(); }
  };
  const setStatus = async (status: string) => {
    if (await run(() => api.patch(`/api/admin/departments/${dept.id}`, { status }))) onChanged();
  };
  const toggle = (m: string) => {
    const next = new Set(modules);
    if (next.has(m)) next.delete(m); else next.add(m);
    setModules(next);
  };

  return (
    <div className="grid two">
      <form className="card form" onSubmit={save}>
        <h2>{dept.name}</h2>
        <label>Display name<input value={name} onChange={(e) => setName(e.target.value)} required minLength={2} /></label>
        <div>
          <h3>Modules</h3>
          <div className="row">
            {template.modules.map((m) => (
              <label key={m} style={{ flexDirection: "row", alignItems: "center", gap: 6, color: "var(--text)" }}>
                <input type="checkbox" style={{ width: "auto" }} checked={modules.has(m)} onChange={() => toggle(m)} /> {m}
              </label>
            ))}
          </div>
          <p className="sub" style={{ marginTop: 6 }}>Assistant tools: {template.tools.join(", ")}</p>
        </div>
        <div className="row">
          <button className="primary" disabled={busy}>Save changes</button>
          <span className="spacer" />
          <button type="button" className={dept.status === "active" ? "danger" : ""} disabled={busy}
                  onClick={() => setStatus(dept.status === "active" ? "archived" : "active")}>
            {dept.status === "active" ? "Archive department" : "Restore department"}
          </button>
        </div>
        {saved && <div className="notice">{saved}</div>}
        {error && <div className="error">{error}</div>}
      </form>
      <div className="card">
        <h2>People in {dept.name}</h2>
        {(members.data ?? []).map((m) => (
          <div key={m.user_id} className="row" style={{ padding: "5px 0" }}>
            <span className="spacer"><strong>{m.display_name}</strong> <span className="sub">{m.title}</span></span>
            {m.roles.map((r) => <span key={r} className="badge">{r}</span>)}
          </div>
        ))}
        {members.data?.length === 0 && <div className="empty">Nobody yet. Assign people under People & roles.</div>}
      </div>
    </div>
  );
}

function NewDepartment({ templates, used, onCreated }: { templates: Template[]; used: Set<string>; onCreated: () => void }) {
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [template, setTemplate] = useState("custom");
  const { busy, error, run } = useAction();
  const chosen = templates.find((t) => t.code === template);

  const create = async (e: FormEvent) => {
    e.preventDefault();
    const finalCode = template === "custom" ? code : template;
    if (await run(() => api.post("/api/admin/departments", { code: finalCode, name, template }))) {
      setCode(""); setName("");
      onCreated();
    }
  };
  return (
    <form className="card form" onSubmit={create}>
      <h2>Add a department</h2>
      <div className="form inline">
        <label>Template
          <select value={template} onChange={(e) => setTemplate(e.target.value)}>
            {templates.filter((t) => t.code === "custom" || !used.has(t.code)).map((t) => (
              <option key={t.code} value={t.code}>{t.name}</option>
            ))}
          </select>
        </label>
        {template === "custom" && (
          <label>Code<input value={code} onChange={(e) => setCode(e.target.value.toLowerCase())} placeholder="quality" pattern="[a-z][a-z0-9_]{1,40}" required /></label>
        )}
        <label>Display name<input value={name} onChange={(e) => setName(e.target.value)} required minLength={2} /></label>
        <button className="primary" disabled={busy}>Create</button>
      </div>
      {chosen && <p className="sub">Modules: {chosen.modules.join(", ")}. Assistant tools: {chosen.tools.join(", ")}.</p>}
      {error && <div className="error">{error}</div>}
    </form>
  );
}
