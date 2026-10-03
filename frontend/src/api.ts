import type { Activity, Analysis, ArchOption, Branch, ChatCard, ChatTurn, ApiToken, Catalog, Diff, Op, Org, Project, ProjectState, Version } from "./types";
import type { Decision, Evaluation } from "./types";
import type { ComponentViews, LldResult } from "./types";

export class ApiError extends Error {
  constructor(public status: number, public data: any) {
    super(typeof data?.detail === "string" ? data.detail : `Request failed (${status})`);
  }
}

async function call<T>(method: string, path: string, body?: unknown, params?: Record<string, string>): Promise<T> {
  const qs = params ? "?" + new URLSearchParams(params).toString() : "";
  const res = await fetch(`/api${path}${qs}`, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, data);
  return data as T;
}

const b = (branch: string) => ({ branch });

export const api = {
  health: () => call<{ ok: boolean; ai_mode: string; model: string; llm?: { available: boolean; groups: Record<string, string[]> } }>("GET", "/health"),
  catalog: () => call<Catalog>("GET", "/catalog"),
  convert: (pid: string, branch: string, target: string, actor: string) =>
    call<{ changed: number; summary: string[]; skipped: string[] }>("POST", `/projects/${pid}/convert`, { target, actor }, b(branch)),
  templates: () => call<{ key: string; name: string }[]>("GET", "/templates"),
  orgs: () => call<Org[]>("GET", "/orgs"),
  mcpInfo: () => call<{ url: string; issuer: string; scopes: string[] }>("GET", "/mcp-info"),
  tokens: (org: string) => call<ApiToken[]>("GET", `/orgs/${org}/tokens`),
  createToken: (org: string, name: string, user_name: string, access: "read" | "write", expires_in_days: number | null) =>
    call<ApiToken & { token: string }>("POST", `/orgs/${org}/tokens`, { name, user_name, access, expires_in_days }),
  revokeToken: (org: string, id: string) => call<null>("DELETE", `/orgs/${org}/tokens/${id}`),
  createOrg: (name: string, owner: string) => call<Org>("POST", "/orgs", { name, owner }),
  projects: (org_id?: string) => call<Project[]>("GET", "/projects", undefined, org_id ? { org_id } : undefined),
  createProject: (name: string, template: string, org_id: string, description = "") =>
    call<Project>("POST", "/projects", { name, template, description, org_id }),
  deleteProject: (pid: string) => call<null>("DELETE", `/projects/${pid}`),
  project: (pid: string, branch: string) =>
    call<{ project: Project; branch: string; branches: Branch[]; head_version: number | null; state: ProjectState }>(
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
};
