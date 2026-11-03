import { FormEvent, useState } from "react";
import { api, day } from "../../api";
import Connections from "../../components/Connections";
import { useAction, useLoad } from "../../hooks";
import type { Member, Project, Task, Workspace } from "../../types";

const TASK_STATUSES = ["todo", "in_progress", "blocked", "done"];
const PROJECT_STATUSES = ["planned", "active", "blocked", "done", "archived"];

export default function Engineering({ ws }: { ws: Workspace }) {
  const projects = useLoad<Project[]>(`/api/engineering/${ws.department_id}/projects`);
  const directory = useLoad<Member[]>("/api/directory");
  const [selected, setSelected] = useState<string | null>(null);
  const canManage = ws.permissions.includes("engineering.project.manage");
  const canWrite = ws.permissions.includes("engineering.task.write");
  const project = projects.data?.find((p) => p.id === selected) ?? null;

  return (
    <div className="grid two">
      <div className="card">
        <h2>Projects</h2>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Code</th><th>Name</th><th>Status</th><th className="num">Open</th><th className="num">Blocked</th></tr></thead>
            <tbody>
              {(projects.data ?? []).map((p) => (
                <tr key={p.id} className={`selectable${p.id === selected ? " selected" : ""}`} onClick={() => setSelected(p.id)}>
                  <td className="mono">
                    <button type="button" className="row-select" aria-pressed={p.id === selected}
                            aria-label={`Open project ${p.code} ${p.name}`} onClick={(e) => { e.stopPropagation(); setSelected(p.id); }}>
                      {p.code}
                    </button>
                  </td><td>{p.name}</td>
                  <td><span className={`badge ${p.status}`}>{p.status}</span></td>
                  <td className="num">{p.open_tasks}</td><td className="num">{p.blocked_tasks}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {projects.data?.length === 0 && <div className="empty">No projects yet.</div>}
        {canManage && <NewProject departmentId={ws.department_id} onCreated={projects.reload} />}
      </div>
      {project ? (
        <ProjectDetail project={project} members={directory.data ?? []} canManage={canManage} canWrite={canWrite}
                       onChanged={projects.reload} />
      ) : (
        <div className="card empty">Select a project to see its tasks, people and connected records.</div>
      )}
    </div>
  );
}

function NewProject({ departmentId, onCreated }: { departmentId: string; onCreated: () => void }) {
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const { busy, error, run } = useAction();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (await run(() => api.post(`/api/engineering/${departmentId}/projects`, { code: code.toUpperCase(), name }))) {
      setCode("");
      setName("");
      onCreated();
    }
  };
  return (
    <form className="form inline" onSubmit={submit} style={{ marginTop: 14 }}>
      <label>Code<input value={code} onChange={(e) => setCode(e.target.value)} placeholder="D12" required /></label>
      <label>Name<input value={name} onChange={(e) => setName(e.target.value)} required /></label>
      <button className="primary" disabled={busy}>Create project</button>
      {error && <div className="error">{error}</div>}
    </form>
  );
}

function ProjectDetail({ project, members, canManage, canWrite, onChanged }: {
  project: Project; members: Member[]; canManage: boolean; canWrite: boolean; onChanged: () => void;
}) {
  const tasks = useLoad<Task[]>(`/api/engineering/projects/${project.id}/tasks`);
  const [title, setTitle] = useState("");
  const [assignee, setAssignee] = useState("");
  const { busy, error, run } = useAction();
  const refresh = () => {
    tasks.reload();
    onChanged();
  };

  const addTask = async (e: FormEvent) => {
    e.preventDefault();
    if (await run(() => api.post(`/api/engineering/projects/${project.id}/tasks`, { title, assignee_id: assignee || null }))) {
      setTitle("");
      refresh();
    }
  };

  return (
    <div className="card">
      <div className="row">
        <h2 className="spacer">{project.code} · {project.name}</h2>
        {canManage ? (
          <select value={project.status} style={{ width: "auto" }}
                  onChange={(e) => run(() => api.patch(`/api/engineering/projects/${project.id}`, { status: e.target.value })).then(onChanged)}>
            {PROJECT_STATUSES.map((s) => <option key={s}>{s}</option>)}
          </select>
        ) : <span className={`badge ${project.status}`}>{project.status}</span>}
      </div>
      <p className="sub">{project.description}</p>
      <table>
        <thead><tr><th>Task</th><th>Assignee</th><th>Due</th><th>Status</th></tr></thead>
        <tbody>
          {(tasks.data ?? []).map((t) => (
            <tr key={t.id}>
              <td>{t.title}</td><td>{t.assignee_name ?? "–"}{t.assignee_active === false && <> <span className="badge disabled">inactive</span></>}</td><td>{day(t.due_date)}</td>
              <td>
                {canWrite ? (
                  <select value={t.status} onChange={(e) => run(() => api.patch(`/api/engineering/tasks/${t.id}`, { status: e.target.value })).then(refresh)}>
                    {TASK_STATUSES.map((s) => <option key={s}>{s}</option>)}
                  </select>
                ) : <span className={`badge ${t.status}`}>{t.status}</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {canWrite && (
        <form className="form inline" onSubmit={addTask} style={{ marginTop: 12 }}>
          <label>New task<input value={title} onChange={(e) => setTitle(e.target.value)} required minLength={2} /></label>
          <label>Assignee
            <select value={assignee} onChange={(e) => setAssignee(e.target.value)}>
              <option value="">Unassigned</option>
              {members.map((m) => <option key={m.user_id} value={m.user_id}>{m.display_name}</option>)}
            </select>
          </label>
          <button className="primary" disabled={busy}>Add task</button>
        </form>
      )}
      {error && <div className="error">{error}</div>}
      <Connections kind="project" refId={project.id} />
    </div>
  );
}
