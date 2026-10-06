export const NODE_TYPES = [
  "client", "gateway", "service", "database", "cache", "queue", "storage",
  "agent", "llm", "mcp", "external", "security", "note",
  // low-level design (inside a component's own diagram)
  "component", "endpoint", "table", "event",
] as const;
export type NodeType = (typeof NODE_TYPES)[number];

export interface Versioned { rev: number; field_revs: Record<string, number> }

export interface ArchNode extends Versioned {
  id: string;
  type: NodeType;
  name: string;
  technology: string;
  vendor?: string;
  catalog_id?: string;
  description: string;
  position: { x: number; y: number };
  requirement_ids: string[];
  props: Record<string, string>;
  /** "" = top-level (HLD) diagram; else the id of the component whose internal (LLD) diagram holds it. */
  parent_id?: string;
}

export interface ArchEdge extends Versioned {
  id: string;
  source: string;
  target: string;
  protocol: string;
  label: string;
}

export type Priority = "low" | "medium" | "high" | "critical";
export type ReqStatus = "draft" | "review" | "approved" | "implemented";

export interface Requirement extends Versioned {
  id: string;
  title: string;
  description: string;
  kind: "functional" | "non_functional";
  category: string;
  priority: Priority;
  actors: string[];
  acceptance_criteria: string[];
  status: ReqStatus;
}

export interface ProjectState {
  rev: number;
  nodes: Record<string, ArchNode>;
  edges: Record<string, ArchEdge>;
  requirements: Record<string, Requirement>;
}

export interface Project { id: string; name: string; description: string; created_at: string; org_id?: string }
export interface CatalogItem {
  id: string; vendor: string; kind: string; name: string; type: NodeType; category: string; kind_label: string;
}
export interface Catalog { vendors: Record<string, string>; items: CatalogItem[] }
export interface Org { id: string; name: string; created_at: string; role?: Role }

// ---- accounts and roles ----
export const ROLES = ["viewer", "reviewer", "developer", "architect", "admin", "owner"] as const;
export type Role = (typeof ROLES)[number];
const RANK: Record<string, number> = Object.fromEntries(ROLES.map((r, i) => [r, i]));
/** Mirrors backend authz.py: read = any member, review = reviewer+, edit = developer+, admin, owner. */
export function can(role: Role | null | undefined, action: "read" | "review" | "edit" | "admin" | "owner"): boolean {
  const min = { read: "viewer", review: "reviewer", edit: "developer", admin: "admin", owner: "owner" }[action];
  return role != null && role in RANK && RANK[role] >= RANK[min];
}
export const ROLE_HELP: Record<Role, string> = {
  viewer: "Read only",
  reviewer: "Read, and accept or reject decisions",
  developer: "Edit the canvas, requirements, versions and decisions; run AI",
  architect: "Same as developer",
  admin: "Also manage members, invites, API tokens and AI keys",
  owner: "Everything, including organization settings and owners",
};
export interface Account { id: string; email: string; name: string; created_at: string; last_login_at: string | null }
export interface Me { user: Account; orgs: (Org & { role: Role })[]; features: { ai_settings: boolean } }
export interface Member { org_id: string; user_id: string; email: string; name: string; role: Role; created_at: string }
export interface Invite {
  id: string; org_id: string; email: string | null; role: Role; created_by: string | null; created_at: string; expires_at: number;
}
export interface InviteInfo { org_name: string; role: Role; email: string | null; expires_at: number }
export type AIProvider = "gemini" | "anthropic" | "openai";
export interface AISettings {
  enabled: boolean;
  providers: Record<AIProvider, { configured: boolean; last4: string | null; updated_at: string | null }>;
}

export interface ApiToken {
  id: string; kind: "pat" | "refresh"; name: string; user_name: string; access: "read" | "write";
  client_name: string; created_at: string; expires_at: number | null; last_used_at: string | null;
}
export interface Branch { name: string; base_version: number | null; created_at: string }
export interface Version { number: number; branch: string; message: string; author: string; parent: number | null; created_at: string }
export interface Activity { actor: string; summary: string[]; rev: number; created_at: string }

export type Op = Record<string, unknown> & { op: string };

export interface Conflict {
  kind: string; id: string; name: string | null; field: string;
  theirs: unknown; yours: unknown; base_rev: number; changed_at_rev: number;
}

export interface Issue {
  severity: "error" | "warning" | "info";
  code: string; message: string; suggestion: string;
  node_ids: string[]; requirement_ids: string[];
}

export interface Analysis {
  requirement_issues: Issue[];
  architecture_issues: Issue[];
  traceability: { requirement_to_nodes: Record<string, string[]>; untraced_requirements: string[]; unjustified_nodes: string[] };
  health: {
    requirements_complete: number; requirements_approved: number; traceability: number; architecture_score: number;
    counts: { requirements: number; components: number; connections: number };
    issues: { errors: number; warnings: number; info: number };
    /** Present when the project has a code analysis: how well the design matches the code (0-100). */
    code_alignment?: number;
  };
  /** Designed-vs-code drift (only when the project has a code analysis). */
  drift_issues?: Issue[];
}

interface DiffSection { added: { id: string; name: string }[]; removed: { id: string; name: string }[]; changed: { id: string; name: string; fields: { field: string; from: unknown; to: unknown }[] }[] }
export interface Diff {
  nodes: DiffSection; edges: DiffSection; requirements: DiffSection;
  total_changes: number; text: string; from_state: ProjectState;
}

export interface User { name: string; color: string }
export interface PresenceUser extends User { client_id: number }

// ---- guided design chat ----
export interface ChatOption { id: string; label: string; description: string; recommended?: boolean }
export interface QuestionCard {
  type: "question"; id: string; question: string; why?: string; multi: boolean;
  options: ChatOption[]; step?: number; total?: number | null;
}
export interface ArchComponent { key: string; name: string; type: NodeType; technology: string; description?: string; layer: number }
export interface ArchOption {
  id: string; name: string; summary: string; pros: string[]; cons: string[]; best_for?: string;
  cost: "low" | "medium" | "high"; complexity: "low" | "medium" | "high"; recommended?: boolean;
  components: ArchComponent[]; connections: { from: string; to: string; protocol?: string }[];
}
export interface ArchitecturesCard { type: "architectures"; id: string; title: string; options: ArchOption[]; requirements: Record<string, unknown>[] }
export type ChatCard = QuestionCard | ArchitecturesCard;
export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
  card?: ChatCard | null;
  answer?: { option_ids: string[]; other?: string };
  applied?: { option: string; branch: string; version: number };
}

// ---- architecture decisions (ADRs) and alternative evaluation ----
export type DecisionStatus = "proposed" | "accepted" | "rejected" | "superseded";
export interface DecisionAlternative { name: string; branch?: string | null; summary: string; pros: string[]; cons: string[] }
export interface Evaluation {
  engine: "jev" | "llm" | "heuristic";
  recommended: string;
  alternatives: string[];
  criteria: string[];
  criteria_labels: Record<string, string>;
  scores: Record<string, Record<string, number>>;
  totals: Record<string, number>;
  rationale: string;
  confidence: number | null;
  notes: string[];
  requirement_ids?: string[];
  probabilities?: Record<string, number>;
  raw?: unknown;
}
export interface Decision {
  id: string; title: string; status: DecisionStatus; context: string; decision: string; consequences: string;
  alternatives: DecisionAlternative[]; chosen: string; requirement_ids: string[]; node_ids: string[];
  evaluation: Evaluation | null; author: string; created_at: string; updated_at: string;
}
// ---- HLD → LLD drill-down ----
export interface LldResult {
  node_id: string; name: string; engine: "llm" | "offline"; counts: Record<string, number>;
  connections: number; summary: string[]; rev: number;
}
export interface ComponentViews {
  node_id: string; name: string; has_lld: boolean;
  api: { endpoints: { id: string; method: string; path: string; description: string; requirement_ids: string[] }[]; openapi: string };
  er: {
    tables: { id: string; name: string; description: string; columns: { name: string; type: string; key: string }[] }[];
    relations: { from: string; to: string; label: string }[]; mermaid: string;
  };
  sequence: { mermaid: string };
}

// ---- repository analysis (code graph) ----
export interface CodeSummary {
  stats: { files?: number; symbols?: number; edges?: number; communities?: number };
  generator: { tool?: string; version?: string };
  root_name: string;
  languages: Record<string, number>;
  infrastructure: { key: string; label: string; category: string; technology: string; files: number; evidence: string[] }[];
  frameworks: { name: string; kind: string; files: number }[];
  entry_points: { path: string; kind: string; name: string }[];
  manifests: { path: string; kind: string; name: string }[];
  compose_services: { name: string; image: string; build: string | null }[];
  dockerfiles: number; k8s: number; terraform: number;
  communities: { id: number; size: number; label: string; paths: string[] }[];
  god_nodes: { id: string; name: string; degree: number }[];
  import_cycles: number;
}
export interface CodeGraphRecord {
  id: string; project_id: string; source: "git" | "local" | "upload"; repo_url: string | null; commit_sha: string | null;
  created_at: string; created_by: string; status: string; summary?: CodeSummary;
}
export interface CodeOverview {
  available: boolean; local_repos: boolean; latest: (CodeGraphRecord & { summary: CodeSummary }) | null;
  graphs: CodeGraphRecord[]; cli: string; install_hint: string | null;
}
export interface CodeJob {
  id: string; project_id: string; status: "queued" | "running" | "done" | "failed"; phase: string; source: string;
  repo_url: string | null; created_by: string; error: string | null; code_graph_id: string | null; commit_sha?: string | null;
}
export interface DriftResult {
  branch: string; code_graph_id: string; repo_url: string | null; commit_sha: string | null; alignment: number;
  issues: (Issue & { code_node_ids: string[] })[];
  matches: { node_id: string; name: string; code_node_id: string; code_name: string; score: number }[];
  code_only: string[]; design_only: string[];
}
export interface ImpactResult {
  branch: string; changed_files: string[]; unmatched_files: string[]; seed_symbols: number;
  affected_symbols: { id: string; name: string; file: string; line: number | null; depth: number; via: string }[];
  affected_symbol_count: number; affected_files: string[];
  code_components: { id: string; name: string }[];
  components: { id: string; name: string; type: string }[];
  requirements: { id: string; title: string; priority: string; status: string }[];
}
export interface ReconstructResult {
  branch: string; version: number; code_graph_id: string; engine: string; components: number; connections: number;
  requirements_linked: number;
}
