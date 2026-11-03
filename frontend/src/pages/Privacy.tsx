import { FormEvent, useState } from "react";
import { api, when } from "../api";
import { PageHeader } from "../components/ui";
import { useAction, useLoad, useMe } from "../hooks";
import type { Conversation } from "../types";

export default function Privacy() {
  const { me } = useMe();
  const chats = useLoad<Conversation[]>("/api/conversations");
  const { busy, error, run } = useAction();
  const names = new Map(me.departments.map((d) => [d.id, d.name]));

  const removeAll = async () => {
    if (!window.confirm("Delete all of your chats? This cannot be undone.")) return;
    await run(() => api.del("/api/conversations"));
    chats.reload();
  };

  return (
    <div className="stack">
      <PageHeader crumbs={[{ label: "Home", to: "/" }, { label: "Account & privacy" }]} title="Account & privacy"
                  description="Your password, and your rights under the GDPR: see, export and delete your data."
                  actions={<a className="btn" href="/api/me/export">Download my data (JSON)</a>} />
      <div className="notice">
        Your chats are private: managers and the owner cannot read them. You can delete them at any time, and your company
        may delete chats automatically after 30 or 60 days. Company records you created (requests, documents, approvals) are
        kept because the company must retain business records; they are not deleted with your chats.
      </div>
      <ChangePassword />
      <div className="card">
        <div className="row">
          <h2 className="spacer">Your chats</h2>
          <button className="danger" disabled={busy || !chats.data?.length} onClick={removeAll}>Delete all chats</button>
        </div>
        {error && <div className="error">{error}</div>}
        <table>
          <thead><tr><th>Title</th><th>Workspace</th><th>Last message</th><th></th></tr></thead>
          <tbody>
            {(chats.data ?? []).map((c) => (
              <tr key={c.id}>
                <td>{c.title}</td>
                <td>{c.department_id ? names.get(c.department_id) ?? "Department" : "Owner"}</td>
                <td>{when(c.last_message_at)}</td>
                <td><button className="small danger" disabled={busy}
                            onClick={() => run(() => api.del(`/api/conversations/${c.id}`)).then(chats.reload)}>Delete</button></td>
              </tr>
            ))}
          </tbody>
        </table>
        {chats.data?.length === 0 && <div className="empty">You have no stored chats.</div>}
      </div>
    </div>
  );
}

function ChangePassword() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [done, setDone] = useState(false);
  const { busy, error, run, setError } = useAction();

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setDone(false);
    if (next !== repeat) {
      setError("The two new passwords differ.");
      return;
    }
    let ok = false;
    await run(async () => {
      await api.post("/api/me/password", { current_password: current, new_password: next });
      ok = true;
    });
    if (ok) {
      setCurrent(""); setNext(""); setRepeat("");
      setDone(true);
    }
  };

  return (
    <form className="card form" onSubmit={submit}>
      <h2>Password</h2>
      <div className="form inline">
        <label>Current password
          <input type="password" autoComplete="current-password" value={current}
                 onChange={(e) => setCurrent(e.target.value)} required />
        </label>
        <label>New password
          <input type="password" autoComplete="new-password" value={next} minLength={12}
                 onChange={(e) => setNext(e.target.value)} required />
        </label>
        <label>Repeat new password
          <input type="password" autoComplete="new-password" value={repeat} minLength={12}
                 onChange={(e) => setRepeat(e.target.value)} required />
        </label>
      </div>
      <p className="sub" style={{ fontSize: "0.8rem" }}>
        At least 12 characters. Changing it signs you out on your other devices; this one stays signed in.
      </p>
      <button className="primary" disabled={busy}>{busy ? "Changing…" : "Change password"}</button>
      {error && <div className="error">{error}</div>}
      {done && <div className="notice" role="status">Your password was changed.</div>}
    </form>
  );
}
