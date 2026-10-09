import type { Analysis, ArchNode, ProjectState } from "./types";

/**
 * Starting prompts for the assistant's Edit and Ask modes, built from what is on the canvas (component names, what is
 * connected to what, open health issues and the current selection) so they always refer to this project.
 */
export interface Suggestions { edit: string[]; ask: string[] }

const MAX = 4;

export function buildSuggestions(state: ProjectState | null, analysis: Analysis | null, selection: string[]): Suggestions {
  if (!state) return { edit: [], ask: [] };
  const nodes = Object.values(state.nodes).filter((n) => !n.parent_id && n.type !== "note");
  if (!nodes.length) {
    return {
      edit: ["Add a web client, an API service and a PostgreSQL database, connected in that order"],
      ask: ["What should I start with for this project?"],
    };
  }
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const edges = Object.values(state.edges).filter((e) => byId.has(e.source) && byId.has(e.target));
  const of = (type: ArchNode["type"]) => nodes.filter((n) => n.type === type);
  const has = (type: ArchNode["type"]) => of(type).length > 0;
  const name = (id: string) => byId.get(id)?.name ?? id;
  const edit: string[] = [];
  const ask: string[] = [];

  // The selection comes first: that is what the person is looking at.
  const selected = selection.map((id) => byId.get(id)).filter((n): n is ArchNode => !!n);
  if (selected.length === 1) {
    const n = selected[0];
    if (n.type === "service" || n.type === "agent") edit.push(`Split ${n.name} into smaller services by responsibility`);
    if (n.type === "database") edit.push(`Add a read replica and daily backups for ${n.name}`);
    if (n.type === "llm" || n.type === "external") edit.push(`Add retries, a timeout and a fallback for calls to ${n.name}`);
    ask.push(`What does ${n.name} do, and what depends on it?`, `What happens if ${n.name} goes down?`);
  } else if (selected.length > 1) {
    edit.push(`Put ${selected.slice(0, 3).map((n) => n.name).join(", ")} behind an API gateway`);
    ask.push(`How do ${selected.slice(0, 3).map((n) => n.name).join(", ")} interact?`);
  }

  // Open health issues are the most useful edits.
  const issues = [...(analysis?.architecture_issues ?? [])].sort((a, b) => (a.severity === "error" ? -1 : 0) - (b.severity === "error" ? -1 : 0));
  for (const i of issues.slice(0, 2)) edit.push(`Fix: ${i.message.replace(/\.$/, "")}`);

  // Gaps in the shape of the architecture, named after the actual components.
  const direct = edges.find((e) => byId.get(e.source)?.type === "service" && byId.get(e.target)?.type === "database");
  if (direct && !has("cache")) edit.push(`Add Redis between ${name(direct.source)} and ${name(direct.target)}`);
  const clientToService = edges.find((e) => byId.get(e.source)?.type === "client" && byId.get(e.target)?.type === "service");
  if (clientToService && !has("gateway")) edit.push(`Add an API gateway between ${name(clientToService.source)} and ${name(clientToService.target)}`);
  const svc = of("service")[0] ?? of("agent")[0];
  if (svc && !has("security")) edit.push(`Add authentication and rate limiting to ${svc.name}`);
  const llmCall = edges.find((e) => byId.get(e.target)?.type === "llm");
  if (llmCall && !has("queue")) edit.push(`Process ${name(llmCall.target)} calls from ${name(llmCall.source)} in a background queue`);
  if (svc && !has("database") && !has("storage")) edit.push(`Add a database so ${svc.name} can store its results`);
  if (!nodes.some((n) => /observ|monitor|logging|grafana|datadog|cloudwatch|sentry/i.test(`${n.name} ${n.technology}`))) {
    edit.push("Add observability: logs, metrics and traces for every service");
  }
  const db = of("database").find((d) => !nodes.some((n) => n.type === "database" && /replica/i.test(n.name) && n.name.startsWith(d.name.split(" ")[0])));
  if (db) edit.push(`Make ${db.name} highly available with a replica in a second zone`);
  const queue = of("queue")[0];
  if (queue && !nodes.some((n) => /dlq|dead.?letter/i.test(n.name))) edit.push(`Add a dead-letter queue for failed ${queue.name} messages`);
  const client = of("client")[0];
  if (client && !nodes.some((n) => /cdn|cloudfront|front ?door|akamai|fastly/i.test(`${n.name} ${n.technology}`))) {
    edit.push(`Serve ${client.name} through a CDN`);
  }

  // Questions about this project.
  const reqs = Object.values(state.requirements);
  const llm = of("llm")[0];
  if (llm) ask.push(`What data is sent to ${llm.name}, and is any of it personal?`);
  if (svc) ask.push(`What are the single points of failure around ${svc.name}?`);
  if (reqs.length) ask.push("Which requirements are not covered by any component?");
  ask.push(`How would this architecture handle 10× the traffic?`);

  return { edit: [...new Set(edit)].slice(0, MAX), ask: [...new Set(ask)].slice(0, MAX) };
}
