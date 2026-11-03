import { FormEvent, useState } from "react";
import { api, currentPeriod, day, euro } from "../../api";
import { useAction, useLoad, useMe } from "../../hooks";
import { DecisionButtons, type Decision } from "../../components/ui";
import type { BudgetLine, PurchaseRequest, Workspace } from "../../types";

export default function Finance({ ws }: { ws: Workspace }) {
  const { me } = useMe();
  const [period, setPeriod] = useState(currentPeriod());
  const budgets = useLoad<BudgetLine[]>(`/api/finance/${ws.department_id}/budgets?period=${period}`);
  const requests = useLoad<PurchaseRequest[]>(`/api/finance/${ws.department_id}/purchase-requests`);
  const { busy, error, run } = useAction();
  const canApprove = ws.permissions.includes("finance.purchase.approve");
  const canManage = ws.permissions.includes("finance.budget.manage");

  const decide = async (id: string, decision: Decision) => {
    if (await run(() => api.post(`/api/finance/${ws.department_id}/purchase-requests/${id}/decision`, { decision }))) {
      requests.reload();
      budgets.reload();
    }
  };

  return (
    <div className="stack">
      <div className="card">
        <div className="row">
          <h2 className="spacer">Budgets</h2>
          <input value={period} onChange={(e) => setPeriod(e.target.value)} style={{ width: 110 }} pattern="\d{4}-Q[1-4]" />
        </div>
        <table>
          <thead><tr><th>Department</th><th className="num">Budget</th><th className="num">Approved</th><th className="num">Pending</th><th className="num">Remaining</th></tr></thead>
          <tbody>
            {(budgets.data ?? []).map((b) => (
              <tr key={b.department_id + b.period}>
                <td>{b.department_name}</td><td className="num">{euro(b.budget_cents)}</td>
                <td className="num">{euro(b.approved_cents)}</td><td className="num">{euro(b.pending_cents)}</td>
                <td className="num"><strong>{euro(b.remaining_cents)}</strong></td>
              </tr>
            ))}
          </tbody>
        </table>
        {budgets.data?.length === 0 && <div className="empty">No budgets for {period}.</div>}
        {canManage && <SetBudget financeId={ws.department_id} period={period} departments={me.departments} onSaved={budgets.reload} />}
      </div>

      <div className="card">
        <h2>Purchase requests</h2>
        {error && <div className="error">{error}</div>}
        <div className="table-wrap">
          <table>
            <thead><tr><th>Request</th><th>Department</th><th>Requested by</th><th className="num">Amount</th><th>Status</th><th></th></tr></thead>
            <tbody>
              {(requests.data ?? []).map((r) => (
                <tr key={r.id}>
                  <td><strong>{r.title}</strong><div className="sub" style={{ fontSize: "0.82rem" }}>{r.justification}</div></td>
                  <td>{r.department_name}<div className="sub">{r.period}</div></td>
                  <td>{r.requester_name}<div className="sub">{day(r.created_at)}</div></td>
                  <td className="num">{euro(r.amount_cents)}</td>
                  <td><span className={`badge ${r.status}`}>{r.status}</span></td>
                  <td>
                    {canApprove && r.status === "submitted" && r.requested_by !== me.user_id && (
                      <DecisionButtons busy={busy} onDecide={(d) => decide(r.id, d)} />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function SetBudget({ financeId, period, departments, onSaved }: {
  financeId: string; period: string; departments: { id: string; name: string }[]; onSaved: () => void;
}) {
  const [dept, setDept] = useState(departments[0]?.id ?? "");
  const [amount, setAmount] = useState("");
  const { busy, error, run } = useAction();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const cents = Math.round(parseFloat(amount.replace(",", ".")) * 100);
    if (await run(() => api.put(`/api/finance/${financeId}/budgets`, { department_id: dept, period, amount_cents: cents }))) {
      setAmount("");
      onSaved();
    }
  };
  return (
    <form className="form inline" onSubmit={submit} style={{ marginTop: 12 }}>
      <label>Department
        <select value={dept} onChange={(e) => setDept(e.target.value)}>
          {departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
        </select>
      </label>
      <label>Budget for {period} (EUR)<input value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" required /></label>
      <button className="primary" disabled={busy}>Set budget</button>
      {error && <div className="error">{error}</div>}
    </form>
  );
}
