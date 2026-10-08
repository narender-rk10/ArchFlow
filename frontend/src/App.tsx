import { useCallback, useEffect, useState } from "react";
import { UNAUTHORIZED_EVENT, api } from "./api";
import { AuthScreen } from "./components/AuthScreen";
import { Landing } from "./components/Landing";
import { ProjectPicker } from "./components/ProjectPicker";
import { Workspace } from "./components/Workspace";
import { ThemeToggle, useTheme } from "./theme";
import { lastBranch } from "./recent";
import type { Me, User } from "./types";

const COLORS = ["#e5484d", "#f76b15", "#ffb224", "#30a46c", "#12a594", "#0090ff", "#6e56cf", "#d6409f"];
const COLOR_KEY = "archflow.color";

/** Your cursor colour is a per-browser preference; your name comes from your account. */
function initialColor(): string {
  try {
    const saved = localStorage.getItem(COLOR_KEY);
    if (saved && COLORS.includes(saved)) return saved;
    const legacy = JSON.parse(localStorage.getItem("archflow.user") ?? "null"); // pre-accounts preference
    if (legacy?.color && COLORS.includes(legacy.color)) return legacy.color;
  } catch { /* ignore */ }
  return COLORS[Math.floor(Math.random() * COLORS.length)];
}

type Route =
  | { kind: "project"; pid: string; branch: string }
  | { kind: "login" | "signup" | "home" }
  | { kind: "invite"; token: string };

function readRoute(): Route {
  const h = location.hash;
  let m = h.match(/^#\/p\/([^/]+)(?:\/(.+))?$/);
  if (m) return { kind: "project", pid: m[1], branch: decodeURIComponent(m[2] ?? "main") };
  m = h.match(/^#\/invite\/([A-Za-z0-9_\-]+)$/);
  if (m) return { kind: "invite", token: m[1] };
  if (h === "#/login") return { kind: "login" };
  if (h === "#/signup") return { kind: "signup" };
  return { kind: "home" };
}

export default function App() {
  const [me, setMe] = useState<Me | null | undefined>(undefined); // undefined = loading, null = signed out
  const [color, setColor] = useState(initialColor);
  const [route, setRoute] = useState(readRoute);
  const [authMode, setAuthMode] = useState<"login" | "signup">(() => (readRoute().kind === "signup" ? "signup" : "login"));
  const [inviteMsg, setInviteMsg] = useState("");
  const theme = useTheme();
  const themeToggle = <ThemeToggle pref={theme.pref} onCycle={theme.cycle} />;

  const refreshMe = useCallback(() => api.me().then(setMe).catch(() => setMe(null)), []);

  useEffect(() => {
    refreshMe();
    const onHash = () => {
      const r = readRoute();
      setRoute(r);
      if (r.kind === "login" || r.kind === "signup") setAuthMode(r.kind);
    };
    const onUnauthorized = () => setMe(null);
    window.addEventListener("hashchange", onHash);
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => {
      window.removeEventListener("hashchange", onHash);
      window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    };
  }, [refreshMe]);

  // Signed in and following an invite link: accept it, then go home.
  useEffect(() => {
    if (!me || route.kind !== "invite") return;
    api.acceptInvite(route.token)
      .then((org) => { setInviteMsg(`You joined ${org.name} as ${org.role}.`); try { localStorage.setItem("archflow.org", org.id); } catch { /* ignore */ } })
      .catch((e) => setInviteMsg((e as Error).message))
      .finally(() => { refreshMe(); location.hash = ""; });
  }, [me?.user.id, route]); // eslint-disable-line react-hooks/exhaustive-deps

  const saveColor = (c: string) => {
    setColor(c);
    try { localStorage.setItem(COLOR_KEY, c); } catch { /* ignore */ }
  };
  const open = (pid: string | null, branch = "main") => {
    location.hash = pid ? `#/p/${pid}/${encodeURIComponent(branch)}` : "";
  };
  const logout = async () => {
    try { await api.logout(); } catch { /* already signed out */ }
    setMe(null);
    location.hash = "#/login";
  };

  if (me === undefined) return <div className="picker"><p className="muted center">Loading…</p></div>;
  if (me === null) {
    if (route.kind === "home") {
      return <Landing themeToggle={themeToggle} onSignIn={() => { location.hash = "#/login"; }} onSignUp={() => { location.hash = "#/signup"; }} />;
    }
    return (
      <AuthScreen mode={authMode} invite={route.kind === "invite" ? route.token : null} themeToggle={themeToggle}
        onMode={(m) => { setAuthMode(m); if (route.kind !== "invite") location.hash = `#/${m}`; }}
        onSignedIn={(m) => { setMe(m); if (route.kind !== "project") location.hash = ""; }} />
    );
  }

  const user: User = { name: me.user.name, color };
  if (route.kind !== "project") {
    return <ProjectPicker me={me} user={user} colors={COLORS} onColor={saveColor} onOpen={(pid) => open(pid, lastBranch(pid))}
      onLogout={logout} onMeChanged={refreshMe} notice={inviteMsg} themeToggle={themeToggle} />;
  }
  return (
    <Workspace
      key={`${route.pid}:${route.branch}`}
      pid={route.pid}
      branch={route.branch}
      user={user}
      me={me}
      colorMode={theme.resolved}
      themeToggle={themeToggle}
      onBranch={(b) => open(route.pid, b)}
      onExit={() => open(null)}
      onOpenProject={(pid, branch) => open(pid, branch)}
      onLogout={logout}
    />
  );
}
