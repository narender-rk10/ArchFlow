/** Recently opened projects, per browser (a convenience: losing it only empties the "Recent" lists). */
export interface RecentProject { pid: string; name: string; orgId: string; branch: string; at: number }

const KEY = "archflow.recent";
const MAX = 8;

export function getRecent(): RecentProject[] {
  try {
    const list = JSON.parse(localStorage.getItem(KEY) ?? "[]");
    return Array.isArray(list) ? list.filter((r) => r && typeof r.pid === "string") : [];
  } catch { return []; }
}

function save(list: RecentProject[]) {
  try { localStorage.setItem(KEY, JSON.stringify(list.slice(0, MAX))); } catch { /* storage unavailable */ }
}

/** Record that a project (and branch) was opened just now. */
export function touchRecent(pid: string, name: string, orgId: string, branch: string) {
  save([{ pid, name, orgId, branch, at: Date.now() }, ...getRecent().filter((r) => r.pid !== pid)]);
}

/** Drop projects that no longer exist or that you can no longer see. */
export function pruneRecent(existing: Set<string>) {
  const list = getRecent();
  const kept = list.filter((r) => existing.has(r.pid));
  if (kept.length !== list.length) save(kept);
}

/** The branch you last used in a project (default main). */
export function lastBranch(pid: string): string {
  return getRecent().find((r) => r.pid === pid)?.branch ?? "main";
}

export function timeAgo(ms: number): string {
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 7 * 86400) return `${Math.floor(s / 86400)} d ago`;
  return new Date(ms).toLocaleDateString();
}
