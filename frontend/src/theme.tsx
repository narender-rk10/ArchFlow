import { useEffect, useState } from "react";

export type ThemePref = "system" | "light" | "dark";
const KEY = "archflow.theme";

export function readTheme(): ThemePref {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

/** Apply a preference to <html data-theme>; "system" removes the attribute so the OS setting decides. */
export function applyTheme(pref: ThemePref) {
  const root = document.documentElement;
  if (pref === "system") delete root.dataset.theme;
  else root.dataset.theme = pref;
}

function systemDark() {
  return typeof matchMedia === "function" && matchMedia("(prefers-color-scheme: dark)").matches;
}

export function useTheme() {
  const [pref, setPref] = useState<ThemePref>(readTheme);
  const [osDark, setOsDark] = useState(systemDark);

  useEffect(() => {
    applyTheme(pref);
    try { if (pref === "system") localStorage.removeItem(KEY); else localStorage.setItem(KEY, pref); } catch { /* ignore */ }
  }, [pref]);

  useEffect(() => {
    if (typeof matchMedia !== "function") return;
    const mq = matchMedia("(prefers-color-scheme: dark)");
    const on = () => setOsDark(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);

  const resolved: "light" | "dark" = pref === "system" ? (osDark ? "dark" : "light") : pref;
  const cycle = () => setPref((p) => (p === "system" ? "light" : p === "light" ? "dark" : "system"));
  return { pref, resolved, setPref, cycle };
}

export function ThemeToggle({ pref, onCycle }: { pref: ThemePref; onCycle: () => void }) {
  const label = pref === "system" ? "Theme: system" : pref === "light" ? "Theme: light" : "Theme: dark";
  const icon = pref === "system" ? "◐" : pref === "light" ? "☀" : "☾";
  return (
    <button type="button" className="theme-toggle" onClick={onCycle} title={`${label} (click to change)`} aria-label={label}>
      <span aria-hidden>{icon}</span>
    </button>
  );
}
