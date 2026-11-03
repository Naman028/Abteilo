import { FormEvent, useState } from "react";
import { api, day } from "../../api";
import { useAction, useLoad, useMe } from "../../hooks";
import { DecisionButtons, type Decision } from "../../components/ui";
import type { AccessRequest, Member, Role, Workspace } from "../../types";

export default function IT({ ws }: { ws: Workspace }) {
  const { me } = useMe();
  const requests = useLoad<AccessRequest[]>(`/api/it/${ws.department_id}/access-requests`);
  const roles = useLoad<Role[]>(`/api/it/${ws.department_id}/roles`);
  const directory = useLoad<Member[]>("/api/directory");
  const { busy, error, run } = useAction();
  const canPropose = ws.permissions.includes("it.access.propose");
  const canApprove = me.company_permissions.includes("it.access.approve");

  const decide = async (id: string, decision: Decision) => {
    if (await run(() => api.post(`/api/it/access-requests/${id}/decision`, { decision }))) requests.reload();
  };

  return (
    <div className="stack">
      <div className="card">
        <h2>Role-change requests</h2>
        <p className="sub">IT proposes, a security administrator approves. Nobody approves their own proposal or their own access.</p>
        {error && <div className="error">{error}</div>}
        <div className="table-wrap">
          <table>
            <thead><tr><th>Person</th><th>Role</th><th>Scope</th><th>Reason</th><th>Proposed by</th><th>Status</th><th></th></tr></thead>
            <tbody>
              {(requests.data ?? []).map((r) => (
                <tr key={r.id}>
                  <td>{r.target_name}</td><td className="mono">{r.role_code}{r.elevated && <> <span className="badge bad" title="Needs the owner's approval">elevated</span></>}</td><td>{r.department_name ?? "Company"}</td>
                  <td>{r.justification}</td><td>{r.proposer_name}<div className="sub">{day(r.created_at)}</div></td>
                  <td><span className={`badge ${r.status}`}>{r.status}</span></td>
                  <td>
                    {canApprove && r.status === "proposed" && (
                      <DecisionButtons busy={busy} onDecide={(d) => decide(r.id, d)} />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {canPropose && <Propose itId={ws.department_id} roles={roles.data ?? []} members={directory.data ?? []}
                                departments={me.departments} onDone={requests.reload} />}
      </div>
      <div className="card">
        <h2>Role catalog</h2>
        <table>
          <thead><tr><th>Role</th><th>Scope</th><th>Grants</th></tr></thead>
          <tbody>
            {(roles.data ?? []).map((r) => (
              <tr key={r.code}><td><strong>{r.name}</strong><div className="mono">{r.code}</div></td><td>{r.scope}</td>
                <td className="mono" style={{ fontSize: "0.78rem" }}>{r.permissions.join(", ")}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Propose({ itId, roles, members, departments, onDone }: {
  itId: string; roles: Role[]; members: Member[]; departments: { id: string; name: string }[]; onDone: () => void;
}) {
  const [target, setTarget] = useState("");
  const [roleCode, setRoleCode] = useState("");
  const [dept, setDept] = useState("");
  const [reason, setReason] = useState("");
  const { busy, error, run } = useAction();
  const role = roles.find((r) => r.code === roleCode);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const body = { target_user_id: target, role_code: roleCode, department_id: role?.scope === "department" ? dept : null, justification: reason };
    if (await run(() => api.post(`/api/it/${itId}/access-requests`, body))) {
      setReason("");
      onDone();
    }
  };

  return (
    <form className="form inline" onSubmit={submit} style={{ marginTop: 14 }}>
      <label>Person
        <select value={target} onChange={(e) => setTarget(e.target.value)} required>
          <option value="">Choose…</option>
          {members.map((m) => <option key={m.user_id} value={m.user_id}>{m.display_name}</option>)}
        </select>
      </label>
      <label>Role
        <select value={roleCode} onChange={(e) => setRoleCode(e.target.value)} required>
          <option value="">Choose…</option>
          {roles.map((r) => <option key={r.code} value={r.code}>{r.name} ({r.scope})</option>)}
        </select>
      </label>
      {role?.scope === "department" && (
        <label>Department
          <select value={dept} onChange={(e) => setDept(e.target.value)} required>
            <option value="">Choose…</option>
            {departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
          </select>
        </label>
      )}
      <label>Reason<input value={reason} onChange={(e) => setReason(e.target.value)} required /></label>
      <button className="primary" disabled={busy}>Propose change</button>
      {error && <div className="error">{error}</div>}
    </form>
  );
}
