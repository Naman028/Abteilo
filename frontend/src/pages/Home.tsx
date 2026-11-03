import { Link } from "react-router-dom";
import { day, euro } from "../api";
import { DeptMark, PageHeader } from "../components/ui";
import { hasCompany, useLoad, useMe } from "../hooks";
import type { LeaveSummary, PurchaseRequest } from "../types";

export default function Home() {
  const { me } = useMe();
  const purchases = useLoad<PurchaseRequest[]>("/api/purchase-requests/mine");
  const leave = useLoad<LeaveSummary>("/api/hr/me/leave");
  const open = (purchases.data ?? []).filter((p) => p.status === "submitted");

  return (
    <div className="stack">
      <PageHeader crumbs={[{ label: "Home" }]} title={`Hello, ${me.display_name.split(" ")[0]}`} description={me.company_name} />

      <div className="grid stats">
        <div className="card stat">
          <div className="value">{leave.data ? leave.data.days_remaining : "–"}</div>
          <div className="label">Leave days remaining ({leave.data?.year ?? ""})</div>
        </div>
        <div className="card stat">
          <div className="value">{open.length}</div>
          <div className="label">Your open purchase requests</div>
        </div>
        <div className="card stat">
          <div className="value">{me.workspaces.length}</div>
          <div className="label">Department workspaces</div>
        </div>
      </div>

      <div className="grid two">
        <div className="card">
          <h2>Your workspaces</h2>
          {me.workspaces.length === 0 && (
            <p className="empty">
              You are not assigned to a department yet.
              {hasCompany(me, "company.settings.manage") && " Use the owner console to manage the company."}
            </p>
          )}
          <div className="stack" style={{ gap: 8 }}>
            {me.workspaces.map((w) => (
              <Link key={w.department_id} to={`/w/${w.department_id}`} className="card row" style={{ padding: "10px 14px", flexWrap: "nowrap" }}>
                <DeptMark template={w.template} name={w.name} />
                <span>
                  <strong>{w.name}</strong>
                  <div className="sub" style={{ fontSize: "0.82rem" }}>{w.modules.join(" · ")}</div>
                </span>
              </Link>
            ))}
          </div>
        </div>
        <div className="card">
          <h2>Recent requests</h2>
          {(purchases.data ?? []).slice(0, 5).map((p) => (
            <div key={p.id} className="row" style={{ padding: "6px 0" }}>
              <span className="spacer">{p.title}</span>
              <span className="mono">{euro(p.amount_cents)}</span>
              <span className={`badge ${p.status}`}>{p.status}</span>
            </div>
          ))}
          {(leave.data?.requests ?? []).slice(0, 3).map((r) => (
            <div key={r.id} className="row" style={{ padding: "6px 0" }}>
              <span className="spacer">Leave {day(r.start_date)} – {day(r.end_date)}</span>
              <span className={`badge ${r.status}`}>{r.status}</span>
            </div>
          ))}
          {purchases.data?.length === 0 && leave.data?.requests.length === 0 && (
            <p className="empty">No requests yet. Purchases and leave are requested under My requests.</p>
          )}
          <Link to="/requests">Manage requests →</Link>
        </div>
      </div>
    </div>
  );
}
