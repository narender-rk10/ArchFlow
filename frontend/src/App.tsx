import { useEffect, useState } from "react";
import { ProjectPicker } from "./components/ProjectPicker";
import { Workspace } from "./components/Workspace";
import { ThemeToggle, useTheme } from "./theme";
import type { User } from "./types";

const COLORS = ["#e5484d", "#f76b15", "#ffb224", "#30a46c", "#12a594", "#0090ff", "#6e56cf", "#d6409f"];

function storage(key: string, value?: string): string | null {
  try {
    if (value !== undefined) localStorage.setItem(key, value);
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function initialUser(): User {
  try {
    const saved = JSON.parse(storage("archflow.user") ?? "null");
    if (saved?.name && saved?.color) return saved;
  } catch { /* ignore */ }
  return { name: "", color: COLORS[Math.floor(Math.random() * COLORS.length)] };
}

function readRoute(): { pid: string | null; branch: string } {
  const m = location.hash.match(/^#\/p\/([^/]+)(?:\/(.+))?$/);
  return m ? { pid: m[1], branch: decodeURIComponent(m[2] ?? "main") } : { pid: null, branch: "main" };
}

export default function App() {
  const [user, setUser] = useState<User>(initialUser);
  const [route, setRoute] = useState(readRoute);
  const theme = useTheme();
  const themeToggle = <ThemeToggle pref={theme.pref} onCycle={theme.cycle} />;

  useEffect(() => {
    const onHash = () => setRoute(readRoute());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const saveUser = (u: User) => {
    setUser(u);
    storage("archflow.user", JSON.stringify(u));
  };

  const open = (pid: string | null, branch = "main") => {
    location.hash = pid ? `#/p/${pid}/${encodeURIComponent(branch)}` : "";
  };

  if (!user.name || !route.pid) {
    return <ProjectPicker user={user} colors={COLORS} onUser={saveUser} onOpen={(pid) => open(pid)} themeToggle={themeToggle} />;
  }
  return (
    <Workspace
      key={`${route.pid}:${route.branch}`}
      pid={route.pid}
      branch={route.branch}
      user={user}
      colorMode={theme.resolved}
      themeToggle={themeToggle}
      onBranch={(b) => open(route.pid, b)}
      onExit={() => open(null)}
    />
  );
}
