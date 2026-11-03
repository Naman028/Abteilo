import { FormEvent, useState } from "react";
import { ApiError, api } from "../api";
import type { Me } from "../types";

export default function Login({ onLogin }: { onLogin: (me: Me) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onLogin(await api.post<Me>("/api/auth/login", { email, password }));
    } catch (err) {
      // A wrong password is a 401 with its own message; anything else means the server could not be reached.
      setError(err instanceof ApiError && err.status < 500 ? err.message
        : "Cannot reach the Abteilo server. Check that it is running, then try again.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-wrap">
      <form className="card login form" onSubmit={submit}>
        <div className="brand">
          <span className="brand-mark">A</span> Abteilo
        </div>
        <p className="sub">The department-aware AI workspace for your company.</p>
        <label>
          Work email
          <input type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required />
        </label>
        <label>
          Password
          <input type="password" autoComplete="current-password" value={password}
                 onChange={(e) => setPassword(e.target.value)} required />
        </label>
        {error && <div className="error">{error}</div>}
        <button className="primary" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
        <p className="sub" style={{ fontSize: "0.8rem" }}>
          Forgot your password? Ask your company owner or an administrator to set a new one for you.
        </p>
        <p className="sub" style={{ fontSize: "0.8rem" }}>
          Only a strictly necessary session cookie is used. No tracking.
        </p>
      </form>
    </div>
  );
}
