import { useEffect, useState } from "react";
import { api } from "../api";
import type { Org, Project, User } from "../types";

interface Props {
  user: User;
  colors: string[];
  onUser: (u: User) => void;
  onOpen: (pid: string) => void;
  themeToggle?: React.ReactNode;
}

export function ProjectPicker({ user, colors, onUser, onOpen, themeToggle }: Props) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [templates, setTemplates] = useState<{ key: string; name: string }[]>([]);
  const [name, setName] = useState("");
  const [template, setTemplate] = useState("ai-customer-support");
  const [draftUser, setDraftUser] = useState(user);
  const [error, setError] = useState("");
  const [aiMode, setAiMode] = useState("");
  const [orgs, setOrgs] = useState<Org[]>([]);
  const [orgId, setOrgId] = useState(() => { try { return localStorage.getItem("archflow.org") ?? "org-default"; } catch { return "org-default"; } });
  const [newOrg, setNewOrg] = useState<string | null>(null);

  useEffect(() => { try { localStorage.setItem("archflow.org", orgId); } catch { /* ignore */ } }, [orgId]);
  useEffect(() => { api.projects(orgId).then(setProjects).catch((e) => setError(String(e.message))); }, [orgId]);
  useEffect(() => {
    api.orgs().then((o) => { setOrgs(o); if (!o.some((x) => x.id === orgId) && o[0]) setOrgId(o[0].id); }).catch(() => {});
    api.templates().then(setTemplates).catch(() => {});
    api.health().then((h) => setAiMode(`${h.ai_mode} · ${h.model}`)).catch(() => setError("Backend is not reachable on :8000."));
  }, []);

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    try {
      const p = await api.createProject(name.trim(), template, orgId);
      onOpen(p.id);
    } catch (err: any) {
      setError(err.message);
    }
  };

  return (
    <div className="picker">
      <header className="picker-head">
        <div className="row between"><div className="brand"><span className="logo">▲</span> ArchFlow</div>{themeToggle}</div>
        <p className="muted">Requirements → Architecture → Canvas → Code. One structured, versioned, collaborative model.</p>
      </header>

      <section className="card">
        <h2>Who are you?</h2>
        <p className="muted small">Your name and colour are shown to collaborators on the canvas.</p>
        <div className="row">
          <input
            value={draftUser.name}
            placeholder="Your name"
            onChange={(e) => setDraftUser({ ...draftUser, name: e.target.value })}
            onBlur={() => draftUser.name.trim() && onUser({ ...draftUser, name: draftUser.name.trim() })}
          />
          <div className="swatches">
            {colors.map((c) => (
              <button
                key={c}
                aria-label={`colour ${c}`}
                className={`swatch ${draftUser.color === c ? "on" : ""}`}
                style={{ background: c }}
                onClick={() => { const u = { ...draftUser, color: c }; setDraftUser(u); if (u.name.trim()) onUser(u); }}
              />
            ))}
          </div>
        </div>
      </section>

      <section className="card">
        <h2>Organization</h2>
        <div className="row">
          <select value={orgId} onChange={(e) => setOrgId(e.target.value)} aria-label="Organization">
            {orgs.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
          </select>
          {newOrg === null ? (
            <button onClick={() => setNewOrg("")}>New organization</button>
          ) : (
            <form className="row" onSubmit={async (e) => {
              e.preventDefault();
              if (!newOrg.trim()) return;
              try {
                const o = await api.createOrg(newOrg.trim(), draftUser.name.trim());
                setOrgs((x) => [...x, o]); setOrgId(o.id); setNewOrg(null);
              } catch (err: any) { setError(err.message); }
            }}>
              <input autoFocus placeholder="Organization name" value={newOrg} onChange={(e) => setNewOrg(e.target.value)} />
              <button className="primary" disabled={!newOrg.trim()}>Create</button>
              <button type="button" onClick={() => setNewOrg(null)}>Cancel</button>
            </form>
          )}
        </div>
      </section>

      <section className="card">
        <h2>New project</h2>
        <form className="row" onSubmit={create}>
          <input value={name} placeholder="e.g. Customer support AI" onChange={(e) => setName(e.target.value)} />
          <select value={template} onChange={(e) => setTemplate(e.target.value)}>
            {templates.map((t) => <option key={t.key} value={t.key}>{t.name}</option>)}
          </select>
          <button className="primary" disabled={!name.trim() || !draftUser.name.trim()}
            onClick={() => onUser({ ...draftUser, name: draftUser.name.trim() })}>Create</button>
        </form>
      </section>

      <section className="card">
        <h2>Projects</h2>
        {projects.length === 0 && <p className="muted">No projects yet.</p>}
        <ul className="project-list">
          {projects.map((p) => (
            <li key={p.id}>
              <button className="link" disabled={!draftUser.name.trim()}
                onClick={() => { onUser({ ...draftUser, name: draftUser.name.trim() }); onOpen(p.id); }}>
                {p.name}
              </button>
              <span className="muted small">{new Date(p.created_at).toLocaleString()}</span>
            </li>
          ))}
        </ul>
        {!draftUser.name.trim() && projects.length > 0 && <p className="muted small">Enter your name to open a project.</p>}
      </section>

      {error && <p className="error">{error}</p>}
      {aiMode && <p className="muted small center">AI engine: {aiMode}</p>}
    </div>
  );
}
