import { useEffect, useState } from "react";
import { api } from "../api";
import { can, type Me, type Project, type User } from "../types";
import { AISettingsDialog, MembersDialog } from "./OrgDialogs";
import { UserMenu } from "./UserMenu";

interface Props {
  me: Me;
  user: User;
  colors: string[];
  onColor: (c: string) => void;
  onOpen: (pid: string) => void;
  onLogout: () => void;
  onMeChanged: () => void;
  notice?: string;
  themeToggle?: React.ReactNode;
}

export function ProjectPicker({ me, user, colors, onColor, onOpen, onLogout, onMeChanged, notice, themeToggle }: Props) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [templates, setTemplates] = useState<{ key: string; name: string }[]>([]);
  const [name, setName] = useState("");
  const [template, setTemplate] = useState("ai-customer-support");
  const [error, setError] = useState("");
  const [aiMode, setAiMode] = useState("");
  const orgs = me.orgs;
  const [orgId, setOrgId] = useState(() => {
    let saved = "";
    try { saved = localStorage.getItem("archflow.org") ?? ""; } catch { /* ignore */ }
    return orgs.some((o) => o.id === saved) ? saved : orgs[0]?.id ?? "";
  });
  const [newOrg, setNewOrg] = useState<string | null>(null);
  const [dialog, setDialog] = useState<"members" | "ai" | null>(null);
  const org = orgs.find((o) => o.id === orgId);
  const role = org?.role;

  useEffect(() => { if (!org && orgs[0]) setOrgId(orgs[0].id); }, [orgs, org]);
  useEffect(() => { try { localStorage.setItem("archflow.org", orgId); } catch { /* ignore */ } }, [orgId]);
  useEffect(() => {
    if (!orgId) { setProjects([]); return; }
    api.projects(orgId).then(setProjects).catch((e) => setError(String(e.message)));
  }, [orgId]);
  useEffect(() => {
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
        <div className="row between">
          <div className="brand"><span className="logo">▲</span> ArchFlow</div>
          <div className="row">{themeToggle}<UserMenu me={me} user={user} colors={colors} onColor={onColor} onLogout={onLogout} /></div>
        </div>
        <p className="muted">Requirements → Architecture → Canvas → Code. One structured, versioned, collaborative model.</p>
      </header>
      {notice && <p className="card small">{notice}</p>}

      <section className="card">
        <h2>Organization</h2>
        <div className="row">
          <select value={orgId} onChange={(e) => setOrgId(e.target.value)} aria-label="Organization">
            {orgs.map((o) => <option key={o.id} value={o.id}>{o.name} · {o.role}</option>)}
          </select>
          {org && <button onClick={() => setDialog("members")}>{can(role, "admin") ? "Members & invites" : "Members"}</button>}
          {org && can(role, "admin") && me.features.ai_settings && <button onClick={() => setDialog("ai")}>AI keys</button>}
          {newOrg === null ? (
            <button onClick={() => setNewOrg("")}>New organization</button>
          ) : (
            <form className="row" onSubmit={async (e) => {
              e.preventDefault();
              if (!newOrg.trim()) return;
              try {
                const o = await api.createOrg(newOrg.trim());
                setOrgId(o.id); setNewOrg(null); onMeChanged();
              } catch (err: any) { setError(err.message); }
            }}>
              <input autoFocus placeholder="Organization name" value={newOrg} onChange={(e) => setNewOrg(e.target.value)} />
              <button className="primary" disabled={!newOrg.trim()}>Create</button>
              <button type="button" onClick={() => setNewOrg(null)}>Cancel</button>
            </form>
          )}
        </div>
        {!orgs.length && <p className="muted small">You are not a member of any organization yet. Create one, or ask for an invite link.</p>}
      </section>

      {org && can(role, "edit") && (
        <section className="card">
          <h2>New project</h2>
          <form className="row" onSubmit={create}>
            <input value={name} placeholder="e.g. Customer support AI" onChange={(e) => setName(e.target.value)} />
            <select value={template} onChange={(e) => setTemplate(e.target.value)}>
              {templates.map((t) => <option key={t.key} value={t.key}>{t.name}</option>)}
            </select>
            <button className="primary" disabled={!name.trim()}>Create</button>
          </form>
        </section>
      )}

      <section className="card">
        <h2>Projects</h2>
        {org && !can(role, "edit") && <p className="muted small">You have {role} access in {org.name}: you can open and review projects but not change them.</p>}
        {projects.length === 0 && <p className="muted">No projects yet.</p>}
        <ul className="project-list">
          {projects.map((p) => (
            <li key={p.id}>
              <button className="link" onClick={() => onOpen(p.id)}>{p.name}</button>
              <span className="muted small">{new Date(p.created_at).toLocaleString()}</span>
            </li>
          ))}
        </ul>
      </section>

      {error && <p className="error">{error}</p>}
      {aiMode && <p className="muted small center">AI engine: {aiMode}</p>}

      {dialog === "members" && org && role && (
        <MembersDialog orgId={org.id} orgName={org.name} role={role} selfId={me.user.id}
          onClose={() => setDialog(null)} onChanged={onMeChanged} />
      )}
      {dialog === "ai" && org && role && (
        <AISettingsDialog orgId={org.id} orgName={org.name} role={role} selfId={me.user.id} onClose={() => setDialog(null)} />
      )}
    </div>
  );
}
