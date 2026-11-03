import { useCallback, useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { ApiError, api } from "./api";
import { MeContext, hasCompany } from "./hooks";
import type { Me } from "./types";
import Login from "./pages/Login";
import Home from "./pages/Home";
import WorkspacePage from "./pages/Workspace";
import MyRequests from "./pages/MyRequests";
import Privacy from "./pages/Privacy";
import OwnerConsole from "./pages/owner/OwnerConsole";
import { DeptMark } from "./components/ui";
import { type Theme, getTheme, setTheme } from "./theme";

const THEMES: [Theme, string][] = [["light", "Light"], ["dark", "Dark"], ["system", "System"]];

function ThemeSwitch() {
  const [theme, choose] = useState<Theme>(getTheme);
  return (
    <div className="theme-switch" role="radiogroup" aria-label="Theme">
      {THEMES.map(([value, label]) => (
        <button key={value} type="button" role="radio" aria-checked={theme === value}
                className={theme === value ? "active" : ""} onClick={() => { setTheme(value); choose(value); }}>
          {label}
        </button>
      ))}
    </div>
  );
}

export default function App() {
  const [me, setMe] = useState<Me | null | undefined>(undefined);

  const refresh = useCallback(() => {
    api.get<Me>("/api/me").then(setMe, (e: ApiError) => (e.status === 401 ? setMe(null) : setMe(null)));
  }, []);
  useEffect(refresh, [refresh]);

  if (me === undefined) return <div className="login-wrap sub">Loading…</div>;
  if (me === null) return <Login onLogin={setMe} />;

  const logout = async () => {
    await api.post("/api/auth/logout");
    setMe(null);
  };
  const isOwner = hasCompany(me, "company.settings.manage") || hasCompany(me, "report.executive.read");

  return (
    <MeContext.Provider value={{ me, refresh }}>
      <div className="shell">
        <aside className="sidebar">
          <div className="brand">
            <span className="brand-mark">A</span> Abteilo
          </div>
          <div className="company">{me.company_name}</div>
          <nav className="nav" aria-label="Main">
            <NavLink to="/" end>Home</NavLink>
            {me.workspaces.length > 0 && <div className="nav-label">Departments</div>}
            {me.workspaces.map((w) => (
              <NavLink key={w.department_id} to={`/w/${w.department_id}`}><DeptMark template={w.template} name={w.name} />{w.name}</NavLink>
            ))}
            <div className="nav-label">Personal</div>
            <NavLink to="/requests">My requests</NavLink>
            <NavLink to="/privacy">Account &amp; privacy</NavLink>
            {isOwner && (
              <>
                <div className="nav-label">Company</div>
                <NavLink to="/owner">Owner console</NavLink>
              </>
            )}
          </nav>
          <div className="sidebar-foot">
            <ThemeSwitch />
            <div className="who">{me.display_name}</div>
            <div className="mono">{me.email}</div>
            <button className="small" style={{ marginTop: 8 }} onClick={logout}>Sign out</button>
          </div>
        </aside>
        <main className="main">
          <Routes>
            <Route path="/" element={<Home />} />
            <Route path="/w/:departmentId" element={<WorkspacePage />} />
            <Route path="/requests" element={<MyRequests />} />
            <Route path="/privacy" element={<Privacy />} />
            {isOwner && <Route path="/owner" element={<OwnerConsole />} />}
            <Route path="*" element={<Navigate to="/" />} />
          </Routes>
        </main>
      </div>
    </MeContext.Provider>
  );
}
