import { FormEvent, useState } from "react";
import { api, day } from "../../api";
import { useAction, useLoad } from "../../hooks";
import { DecisionButtons, type Decision } from "../../components/ui";
import type { LeaveRequest, Member, Workspace } from "../../types";

export default function HR({ ws }: { ws: Workspace }) {
  const pending = useLoad<LeaveRequest[]>(`/api/hr/${ws.department_id}/leave-requests`);
  const directory = useLoad<Member[]>("/api/directory");
  const { busy, error, run } = useAction();

  const decide = async (id: string, decision: Decision) => {
    if (await run(() => api.post(`/api/hr/${ws.department_id}/leave-requests/${id}/decision`, { decision }))) pending.reload();
  };

  return (
    <div className="grid two">
      <div className="card">
        <h2>Leave requests awaiting a decision</h2>
        {error && <div className="error">{error}</div>}
        <table>
          <thead><tr><th>Employee</th><th>Dates</th><th className="num">Days</th><th></th></tr></thead>
          <tbody>
            {(pending.data ?? []).map((r) => (
              <tr key={r.id}>
                <td>{r.employee_name}</td><td>{day(r.start_date)} – {day(r.end_date)}</td><td className="num">{r.days}</td>
                <td>
                  <DecisionButtons busy={busy} onDecide={(d) => decide(r.id, d)} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {pending.data?.length === 0 && <div className="empty">Nothing waiting.</div>}
      </div>
      <SetBalance hrId={ws.department_id} members={directory.data ?? []} />
    </div>
  );
}

function SetBalance({ hrId, members }: { hrId: string; members: Member[] }) {
  const [user, setUser] = useState("");
  const [days, setDays] = useState("30");
  const [saved, setSaved] = useState<string | null>(null);
  const { busy, error, run } = useAction();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const result = await run(() => api.put(`/api/hr/${hrId}/leave-balances`, {
      user_id: user, year: new Date().getFullYear(), days_total: days,
    }));
    if (result) setSaved(members.find((m) => m.user_id === user)?.display_name ?? "");
  };
  return (
    <form className="card form" onSubmit={submit}>
      <h2>Annual leave entitlement</h2>
      <label>Employee
        <select value={user} onChange={(e) => setUser(e.target.value)} required>
          <option value="">Choose…</option>
          {members.map((m) => <option key={m.user_id} value={m.user_id}>{m.display_name}</option>)}
        </select>
      </label>
      <label>Days for {new Date().getFullYear()}<input value={days} onChange={(e) => setDays(e.target.value)} inputMode="decimal" /></label>
      <button className="primary" disabled={busy}>Save entitlement</button>
      {saved !== null && <div className="notice">Saved for {saved}.</div>}
      {error && <div className="error">{error}</div>}
    </form>
  );
}
