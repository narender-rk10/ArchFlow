import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { getRecent, pruneRecent, timeAgo } from "../recent";
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
  const [query, setQuery] = useState("");
  const [allProjects, setAllProjects] = useState<Project[]>([]);
  const searchRef = useRef<HTMLInputElement>(null);
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
  // Every project you can see (all organizations), for "Recently opened" and search.
  useEffect(() => {
    api.projects().then((list) => { setAllProjects(list); pruneRecent(new Set(list.map((p) => p.id))); }).catch(() => {});
  }, [orgs.length]);
  // "/" or Ctrl/Cmd+K focuses the search box.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target as HTMLElement)?.tagName ?? "");
      if ((e.key === "/" && !typing) || ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k")) {
        e.preventDefault();
        searchRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  useEffect(() => {
    api.templates().then(setTemplates).catch(() => {});
    api.health().then((h) => setAiMode(`${h.ai_mode} · ${h.model}`)).catch(() => setError("Backend is not reachable on :8000."));
  }, []);

  const orgNames = useMemo(() => new Map(orgs.map((o) => [o.id, o.name])), [orgs]);
  const q = query.trim().toLowerCase();
  // Searching looks across every organization; otherwise the selected organization's projects are listed.
  const shown = q
    ? allProjects.filter((p) => p.name.toLowerCase().includes(q) || (p.description ?? "").toLowerCase().includes(q)
        || (orgNames.get(p.org_id ?? "") ?? "").toLowerCase().includes(q))
    : projects;
  const recent = useMemo(() => {
    const byId = new Map(allProjects.map((p) => [p.id, p]));
    return getRecent().filter((r) => byId.has(r.pid)).slice(0, 6).map((r) => ({ p: byId.get(r.pid)!, r }));
  }, [allProjects]);

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
        <div className="row between">
          <h2>Projects</h2>
          <input ref={searchRef} className="picker-search" type="search" value={query} onChange={(e) => setQuery(e.target.value)}
            placeholder="Search all projects…  ( / )" aria-label="Search projects" />
        </div>
        {org && !can(role, "edit") && !q && <p className="muted small">You have {role} access in {org.name}: you can open and review projects but not change them.</p>}
        {!q && recent.length > 0 && (
          <>
            <h3 className="picker-sub">Recently opened</h3>
            <div className="project-grid">
              {recent.map(({ p, r }) => (
                <button key={p.id} className="project-card" onClick={() => onOpen(p.id)}>
                  <span className="project-avatar" aria-hidden>{p.name.slice(0, 1).toUpperCase()}</span>
                  <span className="project-card-text">
                    <b>{p.name}</b>
                    <span className="muted tiny">{[orgs.length > 1 ? orgNames.get(p.org_id ?? "") : "", `opened ${timeAgo(r.at)}`,
                      r.branch !== "main" ? `⎇ ${r.branch}` : ""].filter(Boolean).join(" · ")}</span>
                  </span>
                </button>
              ))}
            </div>
            <h3 className="picker-sub">{org ? `All projects in ${org.name}` : "All projects"}</h3>
          </>
        )}
        {shown.length === 0 && <p className="muted">{q ? `No project matches “${query}”.` : "No projects yet."}</p>}
        <div className="project-grid">
          {shown.map((p) => (
            <button key={p.id} className="project-card" onClick={() => onOpen(p.id)}>
              <span className="project-avatar" aria-hidden>{p.name.slice(0, 1).toUpperCase()}</span>
              <span className="project-card-text">
                <b>{p.name}</b>
                <span className="muted tiny">{[q && orgs.length > 1 ? orgNames.get(p.org_id ?? "") : "",
                  p.description, `created ${new Date(p.created_at).toLocaleDateString()}`].filter(Boolean).join(" · ")}</span>
              </span>
            </button>
          ))}
        </div>
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
