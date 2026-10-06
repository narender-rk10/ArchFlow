import { useEffect, useState } from "react";
import { api } from "../api";
import type { InviteInfo, Me } from "../types";

interface Props {
  mode: "login" | "signup";
  invite?: string | null;
  onMode: (mode: "login" | "signup") => void;
  onSignedIn: (me: Me) => void;
  themeToggle?: React.ReactNode;
}

const MIN_PASSWORD = 10;

/** Sign in / create an account. With an invite token, signing up joins that organization. */
export function AuthScreen({ mode, invite, onMode, onSignedIn, themeToggle }: Props) {
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [info, setInfo] = useState<InviteInfo | null>(null);

  useEffect(() => {
    if (!invite) { setInfo(null); return; }
    api.inviteInfo(invite).then((i) => { setInfo(i); if (i.email) setEmail(i.email); })
      .catch((e) => setError((e as Error).message));
  }, [invite]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    if (mode === "signup" && password.length < MIN_PASSWORD) {
      setError(`Password must be at least ${MIN_PASSWORD} characters.`);
      return;
    }
    setBusy(true);
    try {
      let me: Me;
      if (mode === "signup") {
        me = await api.signup(email.trim(), name.trim(), password, invite ?? undefined);
      } else {
        me = await api.login(email.trim(), password);
        if (invite) { await api.acceptInvite(invite); me = await api.me(); }
      }
      onSignedIn(me);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="picker auth">
      <header className="picker-head">
        <div className="row between"><div className="brand"><span className="logo">▲</span> ArchFlow</div>{themeToggle}</div>
        <p className="muted">Requirements → Architecture → Canvas → Code. One structured, versioned, collaborative model.</p>
      </header>
      <form className="card auth-card" onSubmit={submit}>
        <h2>{mode === "signup" ? "Create your account" : "Sign in"}</h2>
        {info && (
          <p className="small invite-note">
            You're invited to <b>{info.org_name}</b> as <b>{info.role}</b>.
            {mode === "login" ? " Sign in to accept, or create an account." : " Create your account to join."}
          </p>
        )}
        <label className="field"><span>Email</span>
          <input type="email" required autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        {mode === "signup" && (
          <label className="field"><span>Your name (shown to collaborators)</span>
            <input required maxLength={80} autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} />
          </label>
        )}
        <label className="field"><span>Password{mode === "signup" ? ` (at least ${MIN_PASSWORD} characters)` : ""}</span>
          <input type="password" required autoComplete={mode === "signup" ? "new-password" : "current-password"}
            minLength={mode === "signup" ? MIN_PASSWORD : undefined} value={password} onChange={(e) => setPassword(e.target.value)} />
        </label>
        {error && <p className="error small" role="alert">{error}</p>}
        <div className="row between">
          <button type="button" className="link" onClick={() => { setError(""); onMode(mode === "signup" ? "login" : "signup"); }}>
            {mode === "signup" ? "Already have an account? Sign in" : "New here? Create an account"}
          </button>
          <button className="primary" disabled={busy}>{busy ? "…" : mode === "signup" ? "Create account" : "Sign in"}</button>
        </div>
      </form>
    </div>
  );
}
