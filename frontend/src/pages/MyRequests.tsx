import { FormEvent, useState } from "react";
import { api, currentPeriod, day, euro } from "../api";
import { PageHeader } from "../components/ui";
import { useAction, useLoad, useMe } from "../hooks";
import type { LeaveSummary, PurchaseRequest } from "../types";

export default function MyRequests() {
  const { me } = useMe();
  const purchases = useLoad<PurchaseRequest[]>("/api/purchase-requests/mine");
  const leave = useLoad<LeaveSummary>("/api/hr/me/leave");
  const { busy, error, run } = useAction();

  return (
    <div className="stack">
      <PageHeader crumbs={[{ label: "Home", to: "/" }, { label: "My requests" }]} title="My requests"
                  description="Your purchase and leave requests, and where each one stands." />
      {error && <div className="error">{error}</div>}
      <div className="grid two">
        <div className="card">
          <h2>Purchase requests</h2>
          {me.workspaces.length > 0 ? <NewPurchase onDone={purchases.reload} /> :
            <p className="empty">You need a department to request purchases.</p>}
          <table style={{ marginTop: 12 }}>
            <tbody>
              {(purchases.data ?? []).map((p) => (
                <tr key={p.id}>
                  <td><strong>{p.title}</strong><div className="sub">{p.department_name} · {p.period}</div>
                    {p.decision_note && <div className="sub">Note: {p.decision_note}</div>}</td>
                  <td className="num">{euro(p.amount_cents)}</td>
                  <td><span className={`badge ${p.status}`}>{p.status}</span></td>
                  <td>{p.status === "submitted" &&
                    <button className="small" disabled={busy}
                            onClick={() => run(() => api.post(`/api/purchase-requests/${p.id}/cancel`)).then(purchases.reload)}>Cancel</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="card">
          <h2>Leave</h2>
          {leave.data && (
            <div className="grid stats" style={{ marginBottom: 12 }}>
              <div className="stat"><div className="value">{leave.data.days_total}</div><div className="label">Entitlement {leave.data.year}</div></div>
              <div className="stat"><div className="value">{leave.data.days_approved}</div><div className="label">Approved</div></div>
              <div className="stat"><div className="value">{leave.data.days_pending}</div><div className="label">Pending</div></div>
              <div className="stat"><div className="value">{leave.data.days_remaining}</div><div className="label">Remaining</div></div>
            </div>
          )}
          <NewLeave onDone={leave.reload} />
          <table style={{ marginTop: 12 }}>
            <tbody>
              {(leave.data?.requests ?? []).map((r) => (
                <tr key={r.id}>
                  <td>{day(r.start_date)} – {day(r.end_date)}</td><td className="num">{r.days} days</td>
                  <td><span className={`badge ${r.status}`}>{r.status}</span></td>
                  <td>{r.status === "submitted" &&
                    <button className="small" disabled={busy}
                            onClick={() => run(() => api.post(`/api/hr/leave-requests/${r.id}/cancel`)).then(leave.reload)}>Cancel</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function NewPurchase({ onDone }: { onDone: () => void }) {
  const { me } = useMe();
  const [dept, setDept] = useState(me.workspaces[0]?.department_id ?? "");
  const [title, setTitle] = useState("");
  const [amount, setAmount] = useState("");
  const [why, setWhy] = useState("");
  const { busy, error, run } = useAction();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const cents = Math.round(parseFloat(amount.replace(",", ".")) * 100);
    if (await run(() => api.post("/api/purchase-requests", {
      department_id: dept, title, justification: why, amount_cents: cents, period: currentPeriod(),
    }))) {
      setTitle(""); setAmount(""); setWhy("");
      onDone();
    }
  };
  return (
    <form className="form" onSubmit={submit}>
      <div className="form inline">
        <label>Department
          <select value={dept} onChange={(e) => setDept(e.target.value)}>
            {me.workspaces.map((w) => <option key={w.department_id} value={w.department_id}>{w.name}</option>)}
          </select>
        </label>
        <label>Item<input value={title} onChange={(e) => setTitle(e.target.value)} required minLength={2} /></label>
        <label>Amount (EUR)<input value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" required /></label>
      </div>
      <label>Justification<textarea value={why} onChange={(e) => setWhy(e.target.value)} required /></label>
      <button className="primary" disabled={busy}>Submit for {currentPeriod()}</button>
      {error && <div className="error">{error}</div>}
    </form>
  );
}

function NewLeave({ onDone }: { onDone: () => void }) {
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const { busy, error, run } = useAction();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (await run(() => api.post("/api/hr/leave-requests", { start_date: start, end_date: end }))) onDone();
  };
  return (
    <form className="form inline" onSubmit={submit}>
      <label>From<input type="date" value={start} onChange={(e) => setStart(e.target.value)} required /></label>
      <label>To<input type="date" value={end} onChange={(e) => setEnd(e.target.value)} required /></label>
      <button className="primary" disabled={busy}>Request leave</button>
      {error && <div className="error">{error}</div>}
    </form>
  );
}
