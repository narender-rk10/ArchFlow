import type { Activity, Analysis, ArchOption, Branch, ChatCard, ChatTurn, ApiToken, Catalog, Diff, Op, Org, Project, ProjectState, Version } from "./types";
import type { Decision, Evaluation } from "./types";
import type { AIProvider, AISettings, Invite, InviteInfo, Me, Member, Role } from "./types";
import type { ComponentViews, LldResult } from "./types";
import type { CodeGraphRecord, CodeJob, CodeOverview, DriftResult, ImpactResult, ReconstructResult } from "./types";

export class ApiError extends Error {
  constructor(public status: number, public data: any) {
    super(typeof data?.detail === "string" ? data.detail : `Request failed (${status})`);
  }
}

/** Fired on any 401 so the app can show the sign-in screen (session expired or signed out elsewhere). */
export const UNAUTHORIZED_EVENT = "archflow:unauthorized";

/** Upload a file as the raw request body (the code-graph JSON can be large; no parsing in the browser). */
async function upload<T>(path: string, file: Blob, params?: Record<string, string>): Promise<T> {
  const qs = params && Object.keys(params).length ? "?" + new URLSearchParams(params).toString() : "";
  const res = await fetch(`/api${path}${qs}`, {
    method: "POST", credentials: "include", headers: { "Content-Type": "application/json" }, body: file,
  });
  const data = await res.json().catch(() => null);
  if (res.status === 401) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  if (!res.ok) throw new ApiError(res.status, data);
  return data as T;
}

async function call<T>(method: string, path: string, body?: unknown, params?: Record<string, string>): Promise<T> {
  const qs = params ? "?" + new URLSearchParams(params).toString() : "";
  const res = await fetch(`/api${path}${qs}`, {
    method,
    credentials: "include", // the session is an HttpOnly cookie
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if (res.status === 401 && !path.startsWith("/auth/")) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  if (!res.ok) throw new ApiError(res.status, data);
  return data as T;
}

const b = (branch: string) => ({ branch });

export const api = {
  // ---- accounts and organizations ----
  me: () => call<Me>("GET", "/me"),
  signup: (email: string, name: string, password: string, invite?: string) =>
    call<Me>("POST", "/auth/signup", { email, name, password, invite }),
  login: (email: string, password: string) => call<Me>("POST", "/auth/login", { email, password }),
  logout: () => call<null>("POST", "/auth/logout"),
  inviteInfo: (token: string) => call<InviteInfo>("GET", `/invites/${encodeURIComponent(token)}`),
  acceptInvite: (token: string) => call<Org>("POST", `/invites/${encodeURIComponent(token)}/accept`),
  updateOrg: (org: string, name: string) => call<Org>("PATCH", `/orgs/${org}`, { name }),
  members: (org: string) => call<Member[]>("GET", `/orgs/${org}/members`),
  updateMember: (org: string, userId: string, role: Role) => call<Member>("PATCH", `/orgs/${org}/members/${userId}`, { role }),
  removeMember: (org: string, userId: string) => call<null>("DELETE", `/orgs/${org}/members/${userId}`),
  invites: (org: string) => call<Invite[]>("GET", `/orgs/${org}/invites`),
  createInvite: (org: string, role: Role, email?: string) =>
    call<Invite & { token: string; link: string }>("POST", `/orgs/${org}/invites`, { role, email: email || null }),
  revokeInvite: (org: string, id: string) => call<null>("DELETE", `/orgs/${org}/invites/${id}`),
  aiSettings: (org: string) => call<AISettings>("GET", `/orgs/${org}/ai-settings`),
  saveAiSettings: (org: string, keys: Partial<Record<AIProvider, string | null>>) =>
    call<AISettings>("PUT", `/orgs/${org}/ai-settings`, keys),
  collabTicket: (pid: string, branch: string, color: string) =>
    call<{ ticket: string; doc: string; read_only: boolean; expires_in: number }>(
      "GET", `/projects/${pid}/collab-ticket`, undefined, { branch, color }),

  health: () => call<{ ok: boolean; ai_mode: string; model: string; llm?: { available: boolean; groups: Record<string, string[]> } }>("GET", "/health"),
  catalog: () => call<Catalog>("GET", "/catalog"),
  convert: (pid: string, branch: string, target: string, actor: string) =>
    call<{ changed: number; summary: string[]; skipped: string[] }>("POST", `/projects/${pid}/convert`, { target, actor }, b(branch)),
  templates: () => call<{ key: string; name: string }[]>("GET", "/templates"),
  orgs: () => call<Org[]>("GET", "/orgs"),
  mcpInfo: () => call<{ url: string; issuer: string; scopes: string[] }>("GET", "/mcp-info"),
  tokens: (org: string) => call<ApiToken[]>("GET", `/orgs/${org}/tokens`),
  createToken: (org: string, name: string, access: "read" | "write", expires_in_days: number | null) =>
    call<ApiToken & { token: string }>("POST", `/orgs/${org}/tokens`, { name, access, expires_in_days }),
  revokeToken: (org: string, id: string) => call<null>("DELETE", `/orgs/${org}/tokens/${id}`),
  createOrg: (name: string) => call<Org>("POST", "/orgs", { name }),
  projects: (org_id?: string) => call<Project[]>("GET", "/projects", undefined, org_id ? { org_id } : undefined),
  createProject: (name: string, template: string, org_id: string, description = "") =>
    call<Project>("POST", "/projects", { name, template, description, org_id }),
  deleteProject: (pid: string) => call<null>("DELETE", `/projects/${pid}`),
  project: (pid: string, branch: string) =>
    call<{ project: Project; branch: string; branches: Branch[]; head_version: number | null; state: ProjectState;
      role: Role; ai_available: boolean }>(
      "GET", `/projects/${pid}`, undefined, b(branch)),
  ops: (pid: string, branch: string, ops: Op[], actor: string) =>
    call<{ state: ProjectState; summary: string[] }>("POST", `/projects/${pid}/ops`, { ops, actor }, b(branch)),
  commit: (pid: string, branch: string, message: string, author: string) =>
    call<Version>("POST", `/projects/${pid}/commit`, { message, author }, b(branch)),
  versions: (pid: string) => call<Version[]>("GET", `/projects/${pid}/versions`),
  activity: (pid: string, branch: string) => call<Activity[]>("GET", `/projects/${pid}/activity`, undefined, b(branch)),
  diff: (pid: string, branch: string, from: string, to = "working") =>
    call<Diff>("GET", `/projects/${pid}/diff`, undefined, { branch, from, to }),
  restore: (pid: string, branch: string, version: number, actor: string) =>
    call<{ state: ProjectState }>("POST", `/projects/${pid}/restore`, { version, actor }, b(branch)),
  createBranch: (pid: string, name: string, from_branch: string, from_version: number | null) =>
    call<{ name: string }>("POST", `/projects/${pid}/branches`, { name, from_branch, from_version }),
  analysis: (pid: string, branch: string) => call<Analysis>("GET", `/projects/${pid}/analysis`, undefined, b(branch)),
  promptTargets: () => call<{ key: string; name: string }[]>("GET", "/prompt-targets"),
  prompt: (pid: string, branch: string, node_ids: string[], requirement_ids: string[], target: string, extra: string) =>
    call<{ prompt: string }>("POST", `/projects/${pid}/prompt`, { node_ids, requirement_ids, target, extra }, b(branch)),
  command: (pid: string, branch: string, text: string, selection: string[], actor: string) =>
    call<{
      applied: boolean; summary?: string[]; explanation: string; questions: string[];
      engine: string; error?: string; state?: ProjectState;
    }>("POST", `/projects/${pid}/ai/command`, { text, selection, actor }, b(branch)),
  chat: (pid: string, branch: string, turns: ChatTurn[], actor: string) =>
    call<{ message: string; card: ChatCard | null; engine: string }>("POST", `/projects/${pid}/chat`, { turns, actor }, b(branch)),
  chatApply: (pid: string, branch: string, option: ArchOption, requirements: Record<string, unknown>[],
    mode: "replace" | "merge" | "branch", actor: string, branch_name?: string) =>
    call<{ branch: string; version: number; summary: string[]; state: ProjectState }>(
      "POST", `/projects/${pid}/chat/apply`, { option, requirements, mode, actor, branch_name }, b(branch)),
  ask: (pid: string, branch: string, question: string) =>
    call<{ answer: string }>("POST", `/projects/${pid}/ai/ask`, { question }, b(branch)),
  // ---- architecture decisions ----
  decisions: (pid: string) => call<Decision[]>("GET", `/projects/${pid}/decisions`),
  decision: (pid: string, id: string) => call<Decision>("GET", `/projects/${pid}/decisions/${id}`),
  createDecision: (pid: string, data: Partial<Decision> & { title: string; actor: string }) =>
    call<Decision>("POST", `/projects/${pid}/decisions`, data),
  updateDecision: (pid: string, id: string, changes: Partial<Decision> & { actor: string }) =>
    call<Decision>("PATCH", `/projects/${pid}/decisions/${id}`, changes),
  adoptDecision: (pid: string, id: string, target_branch: string, actor: string) =>
    call<{ version: number; safety_version: number; branch: string; source_branch: string; decision: Decision }>(
      "POST", `/projects/${pid}/decisions/${id}/adopt`, { target_branch, actor }),
  evaluate: (pid: string, branches: string[], actor: string, create_decision = false) =>
    call<Evaluation & { decision?: Decision }>("POST", `/projects/${pid}/evaluate`, { branches, actor, create_decision }),
  generateLld: (pid: string, branch: string, nodeId: string, actor: string, replace = false) =>
    call<LldResult>("POST", `/projects/${pid}/nodes/${encodeURIComponent(nodeId)}/lld`, { actor, replace }, b(branch)),
  views: (pid: string, branch: string, nodeId: string) =>
    call<ComponentViews>("GET", `/projects/${pid}/nodes/${encodeURIComponent(nodeId)}/views`, undefined, b(branch)),
  // ---- repository analysis (code graph) ----
  codeOverview: (pid: string) => call<CodeOverview>("GET", `/projects/${pid}/codegraph`),
  codeAnalyze: (pid: string, repo_url: string) => call<CodeJob>("POST", `/projects/${pid}/codegraph/analyze`, { repo_url }),
  codeJob: (pid: string, id: string) => call<CodeJob>("GET", `/projects/${pid}/codegraph/jobs/${id}`),
  codeUpload: (pid: string, file: Blob, repo_url?: string, commit_sha?: string) =>
    upload<CodeGraphRecord>(`/projects/${pid}/codegraph/upload`, file,
      Object.fromEntries(Object.entries({ repo_url, commit_sha }).filter(([, v]) => v)) as Record<string, string>),
  codeReconstruct: (pid: string, id: string) =>
    call<ReconstructResult>("POST", `/projects/${pid}/codegraph/${id}/reconstruct`, { refine: true }),
  codeDrift: (pid: string, branch: string) => call<DriftResult>("GET", `/projects/${pid}/codegraph/drift`, undefined, b(branch)),
  codeImpact: (pid: string, branch: string, files: string[]) =>
    call<ImpactResult>("POST", `/projects/${pid}/codegraph/impact`, { files, branch }),
};
