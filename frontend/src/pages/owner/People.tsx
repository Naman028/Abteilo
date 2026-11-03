import { FormEvent, useState } from "react";
import { api, when } from "../../api";
import { useAction, useLoad, useMe } from "../../hooks";

interface MemberRole { assignment_id: string; role_code: string; department_id: string | null; department_name: string | null }
interface MemberAdmin {
  user_id: string; email: string; display_name: string; title: string | null; status: string; last_login_at: string | null;
  roles: MemberRole[];
}

const DEPT_ROLES = ["department_member", "department_editor", "department_lead", "engineering_contributor", "engineering_lead",
  "finance_analyst", "hr_specialist", "it_access_admin"];

export function Members() {
  const members = useLoad<MemberAdmin[]>("/api/admin/members");
  const [openId, setOpenId] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const open = members.data?.find((m) => m.user_id === openId) ?? null;
  const shown = (members.data ?? []).filter((m) =>
    `${m.display_name} ${m.email} ${m.title ?? ""}`.toLowerCase().includes(filter.toLowerCase()));

  return (
    <div className="stack">
      <div className="card">
        <div className="row" style={{ marginBottom: 8 }}>
          <h2 className="spacer">People & roles</h2>
          <input placeholder="Search people…" value={filter} onChange={(e) => setFilter(e.target.value)} style={{ maxWidth: 240 }} />
        </div>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Name</th><th>Roles</th><th>Last sign-in</th><th>Status</th><th></th></tr></thead>
            <tbody>
              {shown.map((m) => (
                <tr key={m.user_id} style={m.user_id === openId ? { background: "var(--surface-2)" } : undefined}>
                  <td><strong>{m.display_name}</strong><div className="sub">{m.title}</div><div className="mono">{m.email}</div></td>
                  <td>{m.roles.map((r) => (
                    <div key={r.assignment_id} className="mono">{r.role_code}{r.department_name ? ` @ ${r.department_name}` : ""}</div>
                  ))}</td>
                  <td>{m.last_login_at ? when(m.last_login_at) : "never"}</td>
                  <td><span className={`badge ${m.status}`}>{m.status}</span></td>
                  <td><button className="small" onClick={() => setOpenId(m.user_id === openId ? null : m.user_id)}>{m.user_id === openId ? "Close" : "Manage"}</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="sub" style={{ marginTop: 8 }}>
          Department roles are assigned here. Company-wide roles go through the IT access-request workflow, and owner or
          security-administrator roles also need an owner's approval.
        </p>
      </div>
      {open && <PersonDetail key={open.user_id} person={open} onChanged={members.reload} />}
      <NewPerson onCreated={members.reload} />
    </div>
  );
}

function PersonDetail({ person, onChanged }: { person: MemberAdmin; onChanged: () => void }) {
  const { me } = useMe();
  const self = person.user_id === me.user_id;
  const [displayName, setDisplayName] = useState(person.display_name);
  const [title, setTitle] = useState(person.title ?? "");
  const [dept, setDept] = useState(me.departments[0]?.id ?? "");
  const [role, setRole] = useState("department_member");
  const [password, setPassword] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const { busy, error, run } = useAction();
  const base = `/api/admin/members/${person.user_id}`;
  const after = (msg: string) => (ok: unknown) => { if (ok !== undefined) { setNotice(msg); onChanged(); } };

  const saveDetails = (e: FormEvent) => {
    e.preventDefault();
    run(() => api.patch(base, { display_name: displayName, title })).then(after("Details saved."));
  };
  const assign = (e: FormEvent) => {
    e.preventDefault();
    run(() => api.post(`${base}/roles`, { department_id: dept, role_code: role })).then(after("Role assigned."));
  };
  const resetPassword = async (e: FormEvent) => {
    e.preventDefault();
    const ok = await run(async () => { await api.post(`${base}/password`, { new_password: password }); return true; });
    if (ok) { setPassword(""); setNotice("Password reset. The person was signed out everywhere."); }
  };

  return (
    <div className="grid two">
      <div className="card stack">
        <h2>{person.display_name}</h2>
        <form className="form" onSubmit={saveDetails}>
          <div className="form inline">
            <label>Name<input value={displayName} onChange={(e) => setDisplayName(e.target.value)} required minLength={2} /></label>
            <label>Job title<input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={80} /></label>
            <button disabled={busy}>Save details</button>
          </div>
        </form>
        {!self && (
          <form className="form" onSubmit={resetPassword}>
            <div className="form inline">
              <label>New initial password (min. 12)<input type="password" value={password} onChange={(e) => setPassword(e.target.value)} minLength={12} required /></label>
              <button disabled={busy}>Reset password</button>
            </div>
          </form>
        )}
        {!self && (
          <div className="row">
            <button className={person.status === "active" ? "danger" : ""} disabled={busy}
                    onClick={() => run(() => api.patch(base, { status: person.status === "active" ? "disabled" : "active" }))
                      .then(after(person.status === "active" ? "Account disabled and signed out." : "Account enabled."))}>
              {person.status === "active" ? "Disable account" : "Enable account"}
            </button>
          </div>
        )}
        {notice && <div className="notice">{notice}</div>}
        {error && <div className="error">{error}</div>}
      </div>
      <div className="card stack">
        <h2>Roles</h2>
        {person.roles.map((r) => (
          <div key={r.assignment_id} className="row">
            <span className="mono spacer">{r.role_code}{r.department_name ? ` @ ${r.department_name}` : " (company)"}</span>
            {!self && (
              <button className="small danger" disabled={busy}
                      onClick={() => run(() => api.del(`${base}/roles/${r.assignment_id}`)).then(() => { setNotice("Role revoked."); onChanged(); })}>
                Revoke
              </button>
            )}
          </div>
        ))}
        <form className="form inline" onSubmit={assign}>
          <label>Department
            <select value={dept} onChange={(e) => setDept(e.target.value)}>
              {me.departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
            </select>
          </label>
          <label>Department role
            <select value={role} onChange={(e) => setRole(e.target.value)}>
              {DEPT_ROLES.map((r) => <option key={r}>{r}</option>)}
            </select>
          </label>
          <button className="primary" disabled={busy}>Assign</button>
        </form>
      </div>
    </div>
  );
}

function NewPerson({ onCreated }: { onCreated: () => void }) {
  const { me } = useMe();
  const empty = { email: "", display_name: "", title: "", initial_password: "", department_id: "", department_role: "department_member" };
  const [form, setForm] = useState(empty);
  const { busy, error, run } = useAction();
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value });

  const create = async (e: FormEvent) => {
    e.preventDefault();
    const body = { ...form, title: form.title || null, department_id: form.department_id || null,
                   department_role: form.department_id ? form.department_role : null };
    if (await run(() => api.post("/api/admin/members", body))) {
      setForm(empty);
      onCreated();
    }
  };
  return (
    <form className="card form" onSubmit={create}>
      <h2>Add a person</h2>
      <div className="form inline">
        <label>Name<input value={form.display_name} onChange={set("display_name")} required minLength={2} /></label>
        <label>Work email<input type="email" value={form.email} onChange={set("email")} required /></label>
        <label>Job title<input value={form.title} onChange={set("title")} /></label>
        <label>Initial password (min. 12)<input type="password" value={form.initial_password} onChange={set("initial_password")} minLength={12} required /></label>
        <label>Department
          <select value={form.department_id} onChange={set("department_id")}>
            <option value="">None yet</option>
            {me.departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
          </select>
        </label>
        {form.department_id && (
          <label>Department role
            <select value={form.department_role} onChange={set("department_role")}>
              {DEPT_ROLES.map((r) => <option key={r}>{r}</option>)}
            </select>
          </label>
        )}
      </div>
      <button className="primary" disabled={busy}>Add person</button>
      {error && <div className="error">{error}</div>}
    </form>
  );
}
