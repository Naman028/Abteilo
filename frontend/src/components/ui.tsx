import { KeyboardEvent, ReactNode, useRef } from "react";
import { Link } from "react-router-dom";

/** Breadcrumb + title + description, the start of every routed page. */
export function PageHeader({ crumbs, title, description, meta, actions }: {
  crumbs: { label: string; to?: string }[];
  title: ReactNode;
  description?: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="page-head">
      <div>
        <nav className="breadcrumb" aria-label="Breadcrumb">
          <ol>
            {crumbs.map((c, i) => (
              <li key={c.label}>
                {i === crumbs.length - 1 ? <span aria-current="page">{c.label}</span>
                  : c.to ? <Link to={c.to}>{c.label}</Link> : c.label}
              </li>
            ))}
          </ol>
        </nav>
        <h1>{title}</h1>
        {description && <div className="sub">{description}</div>}
        {meta && <div className="meta">{meta}</div>}
      </div>
      {actions && <div className="row">{actions}</div>}
    </header>
  );
}

/** ARIA tablist with roving focus: arrow keys, Home and End move between tabs. */
export function Tabs({ id, tabs, active, onChange }: {
  id: string;
  tabs: [key: string, label: string][];
  active: string;
  onChange: (key: string) => void;
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const onKey = (e: KeyboardEvent, i: number) => {
    const last = tabs.length - 1;
    const next = { ArrowRight: i === last ? 0 : i + 1, ArrowLeft: i === 0 ? last : i - 1, Home: 0, End: last }[e.key];
    if (next === undefined) return;
    e.preventDefault();
    refs.current[next]?.focus();
    onChange(tabs[next][0]);
  };
  return (
    <div className="tabs" role="tablist">
      {tabs.map(([key, label], i) => (
        <button key={key} ref={(el) => { refs.current[i] = el; }} role="tab" id={`${id}-tab-${key}`}
                aria-selected={active === key} aria-controls={`${id}-panel`} tabIndex={active === key ? 0 : -1}
                className={active === key ? "active" : ""} onClick={() => onChange(key)} onKeyDown={(e) => onKey(e, i)}>
          {label}
        </button>
      ))}
    </div>
  );
}

export function TabPanel({ id, active, children }: { id: string; active: string; children: ReactNode }) {
  return <div role="tabpanel" id={`${id}-panel`} aria-labelledby={`${id}-tab-${active}`}>{children}</div>;
}

/** KPI tile: a button when it navigates, plain text otherwise. */
export function Stat({ value, label, onClick }: { value: ReactNode; label: string; onClick?: () => void }) {
  const body = (<><div className="value">{value}</div><div className="label">{label}</div></>);
  return onClick
    ? <button type="button" className="card stat" onClick={onClick}>{body}</button>
    : <div className="card stat">{body}</div>;
}

export type Decision = "approve" | "reject";

/** The Approve / Reject pair used by every approval queue. */
export function DecisionButtons({ busy, onDecide }: { busy: boolean; onDecide: (decision: Decision) => void }) {
  return (
    <div className="row">
      <button className="small primary" disabled={busy} onClick={() => onDecide("approve")}>Approve</button>
      <button className="small danger" disabled={busy} onClick={() => onDecide("reject")}>Reject</button>
    </div>
  );
}

const MARKS: Record<string, string> = { engineering: "EN", finance: "FI", human_resources: "HR", it_security: "IT" };

/** Neutral two-letter monogram that identifies a department (not its status). */
export function DeptMark({ template, name }: { template: string; name: string }) {
  const mark = MARKS[template] ?? name.split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();
  return <span className="dept-mark" aria-hidden="true">{mark}</span>;
}

export const DEPARTMENT_PURPOSE: Record<string, string> = {
  engineering: "Projects, tasks and technical documents, with the relationships between them.",
  finance: "Budgets, purchase requests and approvals for this department.",
  human_resources: "Leave requests, balances and HR policies.",
  it_security: "Role changes, access requests and security policies.",
  custom: "Department documents and the department assistant.",
};
