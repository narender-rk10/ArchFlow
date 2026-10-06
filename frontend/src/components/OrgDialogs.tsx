import { useEffect, useState } from "react";
import { ApiError, api } from "../api";
import { ROLES, ROLE_HELP, can, type AIProvider, type AISettings, type Invite, type Member, type Role } from "../types";
import { Copy } from "./ConnectDialog";

interface Base { orgId: string; orgName: string; role: Role; selfId: string; onClose: () => void; onChanged?: () => void }

function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal wide" role="dialog" aria-label={title} onClick={(e) => e.stopPropagation()}>
        <div className="row between">
          <h3>{title}</h3>
          <button className="ghost" onClick={onClose} aria-label="close">✕</button>
        </div>
        {children}
      </div>
    </div>
  );
}

/** Roles the current user may hand out: admins up to admin, owners anything. */
const grantable = (mine: Role) => ROLES.filter((r) => r !== "owner" || mine === "owner");

/** Members and invites. Admins and owners manage; everyone else sees the list read-only. */
export function MembersDialog({ orgId, orgName, role, selfId, onClose, onChanged }: Base) {
  const manage = can(role, "admin");
  const [members, setMembers] = useState<Member[]>([]);
  const [invites, setInvites] = useState<Invite[]>([]);
  const [inviteRole, setInviteRole] = useState<Role>("developer");
  const [inviteEmail, setInviteEmail] = useState("");
  const [link, setLink] = useState<string | null>(null);
  const [error, setError] = useState("");

  const load = () => {
    api.members(orgId).then(setMembers).catch((e) => setError(e.message));
    if (manage) api.invites(orgId).then(setInvites).catch(() => {});
  };
  useEffect(load, [orgId]); // eslint-disable-line react-hooks/exhaustive-deps

  const run = async (fn: () => Promise<unknown>) => {
    setError("");
    try { await fn(); load(); onChanged?.(); } catch (e) { setError((e as ApiError).message); }
  };
  const createInvite = (e: React.FormEvent) => {
    e.preventDefault();
    run(async () => {
      const inv = await api.createInvite(orgId, inviteRole, inviteEmail.trim() || undefined);
      setLink(inv.link);
      setInviteEmail("");
    });
  };

  return (
    <Modal title={`Members of ${orgName}`} onClose={onClose}>
      <table className="tokens members">
        <thead><tr><th>Name</th><th>Email</th><th>Role</th><th /></tr></thead>
        <tbody>
          {members.map((m) => {
            const editable = manage && (role === "owner" || m.role !== "owner");
            return (
              <tr key={m.user_id}>
                <td>{m.name}{m.user_id === selfId && <span className="muted small"> (you)</span>}</td>
                <td className="small">{m.email}</td>
                <td>
                  {editable ? (
                    <select value={m.role} aria-label={`Role of ${m.name}`} title={ROLE_HELP[m.role]}
                      onChange={(e) => run(() => api.updateMember(orgId, m.user_id, e.target.value as Role))}>
                      {grantable(role).map((r) => <option key={r} value={r}>{r}</option>)}
                    </select>
                  ) : <span title={ROLE_HELP[m.role]}>{m.role}</span>}
                </td>
                <td>
                  {(editable || m.user_id === selfId) && (
                    <button className="small danger" onClick={() => {
                      const leaving = m.user_id === selfId;
                      if (window.confirm(leaving ? `Leave ${orgName}?` : `Remove ${m.name} from ${orgName}? Their API tokens for it stop working.`)) {
                        run(() => api.removeMember(orgId, m.user_id));
                      }
                    }}>{m.user_id === selfId ? "Leave" : "Remove"}</button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {manage ? (
        <>
          <h4>Invite people</h4>
          <p className="muted small">Creates a single-use link valid for 7 days. Send it to the person; they sign up (or sign in) to join.</p>
          <form className="row gap" onSubmit={createInvite}>
            <input className="grow" type="email" placeholder="Email (optional: restricts who can use it)" value={inviteEmail}
              onChange={(e) => setInviteEmail(e.target.value)} aria-label="Invitee email" />
            <select value={inviteRole} onChange={(e) => setInviteRole(e.target.value as Role)} aria-label="Role" title={ROLE_HELP[inviteRole]}>
              {grantable(role).map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
            <button className="primary">Create invite link</button>
          </form>
          {link && (
            <div className="token-created">
              <span className="small"><b>Copy this link now.</b> It won't be shown again.</span>
              <div className="snippet"><pre>{link}</pre><Copy text={link} /></div>
            </div>
          )}
          {invites.length > 0 && (
            <table className="tokens">
              <thead><tr><th>Pending invite</th><th>Role</th><th>Expires</th><th /></tr></thead>
              <tbody>
                {invites.map((i) => (
                  <tr key={i.id}>
                    <td className="small">{i.email ?? "anyone with the link"}</td>
                    <td>{i.role}</td>
                    <td className="small">{new Date(i.expires_at * 1000).toLocaleDateString()}</td>
                    <td><button className="small" onClick={() => run(() => api.revokeInvite(orgId, i.id))}>Revoke</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      ) : <p className="muted small">Only admins and owners can invite people or change roles.</p>}
      <details className="small muted roles-help">
        <summary>What each role can do</summary>
        <ul>{ROLES.map((r) => <li key={r}><b>{r}</b>: {ROLE_HELP[r]}</li>)}</ul>
      </details>
      {error && <p className="error small">{error}</p>}
    </Modal>
  );
}

const PROVIDERS: { key: AIProvider; label: string; hint: string }[] = [
  { key: "gemini", label: "Google Gemini", hint: "Primary model (aistudio.google.com/apikey)" },
  { key: "anthropic", label: "Anthropic Claude", hint: "First fallback" },
  { key: "openai", label: "OpenAI", hint: "Second fallback" },
];

/** Bring-your-own LLM keys for the organization (admins). Keys are write-only: only the last 4 characters come back. */
export function AISettingsDialog({ orgId, orgName, onClose }: Base) {
  const [settings, setSettings] = useState<AISettings | null>(null);
  const [draft, setDraft] = useState<Partial<Record<AIProvider, string>>>({});
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => { api.aiSettings(orgId).then(setSettings).catch((e) => setError(e.message)); }, [orgId]);

  const save = async (changes: Partial<Record<AIProvider, string | null>>) => {
    setError(""); setSaved(false);
    try { setSettings(await api.saveAiSettings(orgId, changes)); setDraft({}); setSaved(true); }
    catch (e) { setError((e as Error).message); }
  };
  const pending = Object.fromEntries(Object.entries(draft).filter(([, v]) => v && v.trim()));

  return (
    <Modal title={`AI provider keys · ${orgName}`} onClose={onClose}>
      <p className="muted small">AI features for this organization use these keys instead of the server's. They are encrypted at
        rest and never shown again; leave a field empty to keep the current key.</p>
      {!settings && !error && <p className="muted small">Loading…</p>}
      {settings && (
        <form onSubmit={(e) => { e.preventDefault(); save(pending); }}>
          {PROVIDERS.map((p) => {
            const cur = settings.providers[p.key];
            return (
              <div key={p.key} className="field">
                <span>{p.label} <span className="muted tiny">· {p.hint}</span></span>
                <div className="row gap">
                  <input className="grow" type="password" autoComplete="off" aria-label={`${p.label} API key`}
                    placeholder={cur.configured ? `Configured (…${cur.last4})` : "Not set: the server's key is used"}
                    value={draft[p.key] ?? ""} onChange={(e) => setDraft({ ...draft, [p.key]: e.target.value })} />
                  {cur.configured && <button type="button" className="small danger" onClick={() => save({ [p.key]: null })}>Remove</button>}
                </div>
              </div>
            );
          })}
          <div className="row end gap">
            {saved && <span className="small ok">Saved</span>}
            <button className="primary" disabled={!Object.keys(pending).length}>Save keys</button>
          </div>
        </form>
      )}
      {error && <p className="error small">{error}</p>}
    </Modal>
  );
}
