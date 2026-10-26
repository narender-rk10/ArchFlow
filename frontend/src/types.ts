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
export type AIProvider = string; // gemini, anthropic, openai, openrouter, mistral, groq, deepseek, xai, azure, openai_compatible
export type AIGroup = "reasoning" | "fast";
export interface AIProviderInfo {
  id: AIProvider; label: string; key: "required" | "optional"; params: Record<string, string>; required_params: string[];
  docs: string | null; examples: Record<AIGroup, string>; server_key: boolean;
}
export interface AIModelChoice { provider: AIProvider; model: string }
export interface AIConfig { reasoning: AIModelChoice[]; fast: AIModelChoice[]; params: Record<AIProvider, Record<string, string>> }
export interface AISettings {
  /** False when the server cannot encrypt keys (ARCHFLOW_SECRET_KEY and no data directory): keys cannot be saved. */
  enabled: boolean;
  providers: Record<AIProvider, { configured: boolean; last4: string | null; updated_at: string | null }>;
  catalog: AIProviderInfo[]; config: AIConfig; defaults: Record<AIGroup, string[]>; effective: Record<AIGroup, string[]>;
  available: boolean;
}
export interface AITestResult { ok: boolean; group: AIGroup; model?: string; chain?: string[]; latency_ms?: number; reply?: string; error?: string }

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
  /** NFR-derived criterion weights behind `totals` (older evaluations lack them). */
  weights?: Record<string, number>;
  details?: Record<string, EvaluationDetail>;
  deciding_factors?: string[];
  flip_conditions?: string;
  /** With Jev scoring: the LLM agent's own pick, run in parallel, and whether it agrees. */
  second_opinion?: { engine: "llm"; recommended: string; totals: Record<string, number>; scores: Record<string, Record<string, number>>;
    rationale: string; confidence: number | null; agrees: boolean };
  /** Questions the AI suggests asking next about this comparison. */
  follow_up_questions?: string[];
}
export interface EvaluationDetail {
  summary: string;
  strengths: string[];
  risks: string[];
  best_when: string;
  reasons: Record<string, string>;
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
  compose_services: { name: string; image: string; build: string | null; files?: string[]; dev_tool?: boolean }[];
  dev_tools?: { name: string; image: string; files: string[] }[];  // excluded from the reconstructed architecture
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
  dev_tools?: { name: string; image: string; files: string[]; included: boolean }[];
  excluded?: { path: string; role: string; reason: string; files: number }[];
}

// ---- conversations (shared AI chat threads) and project memory ----
export type ConversationKind = "assistant" | "design";
export interface Conversation {
  id: string; project_id: string; branch: string; kind: ConversationKind; title: string;
  /** Running summary of older turns the AI no longer sees verbatim. */
  summary: string; summarized_seq: number;
  created_by: string; created_by_id: string | null; created_at: string; updated_at: string; message_count: number;
}
export interface ConversationMessage {
  seq: number; role: "user" | "assistant"; mode: "ask" | "command" | "design" | "note"; content: string;
  data: {
    engine?: string; error?: string; applied?: boolean | { option: string; branch: string; version: number };
    summary?: string[]; questions?: string[]; question_options?: Record<string, string[]>; selection?: string[];
    card?: ChatCard | null; answer?: { option_ids: string[]; other?: string };
  };
  author: string; created_at: string;
}
export const MEMORY_CATEGORIES = ["constraint", "preference", "decision", "requirement", "context", "fact"] as const;
export type MemoryCategory = (typeof MEMORY_CATEGORIES)[number];
export interface MemoryItem {
  id: string; text: string; category: MemoryCategory; pinned: boolean;
  source: "user" | "ai" | "design-chat" | "mcp"; conversation_id: string | null;
  created_by: string; created_at: string; updated_at: string;
}

// ---- OpenSpec (spec-driven changes, stored per project) ----
export interface OpenSpecIssue { level: "ERROR" | "WARNING" | "INFO"; path?: string; message: string }
export interface OpenSpecChange {
  change_id: string; title: string; status: "draft" | "valid" | "invalid" | "archived"; engine: string; branch: string;
  scope: { node_ids?: string[]; requirement_ids?: string[]; whole_system?: boolean; target?: string; extra?: string };
  validation: { valid?: boolean; issues?: OpenSpecIssue[]; repairs?: number; notes?: string[]; skipped?: string;
    archive?: { totals?: Record<string, number> } };
  archived_as: string | null; created_by: string; created_at: string; updated_at: string;
}
export interface OpenSpecOverview {
  available: boolean; ai: boolean; changes: OpenSpecChange[];
  specs: { capability: string; path: string; updated_at: string; updated_by: string }[];
}
export interface OpenSpecJob {
  id: string; status: "running" | "done" | "failed"; step: string; steps: { step: string; at: number }[];
  change_id: string | null; error: string | null; elapsed?: number;
}

// ---- AI usage and budgets ----
export interface AIUsageRow { key: string; calls: number; prompt_tokens: number; completion_tokens: number; cost_usd: number; name?: string }
export interface AIUsageReport {
  days: number; month_start: string; month_spend_usd: number; used_percent: number | null; status: "ok" | "warning" | "blocked";
  budget: { monthly_limit_usd: number | null; alert_percent: number; updated_by: string | null; updated_at: string | null };
  total: { calls: number; prompt_tokens: number; completion_tokens: number; cost_usd: number };
  by_feature: AIUsageRow[]; by_model: AIUsageRow[]; by_project: AIUsageRow[]; daily: AIUsageRow[];
  /** Calls per prompt version (id@version); missing on servers before prompt versioning. */
  by_prompt?: AIUsageRow[];
  /** Calls per person (name filled in); missing on older servers. */
  by_user?: AIUsageRow[];
}

// ---- AI architecture review (specialist agents in parallel, see review.py) ----
export interface ReviewFinding {
  focus: string; severity: "critical" | "high" | "medium" | "low"; title: string; detail: string;
  components: string[]; requirement_ids: string[]; fix: string;
}
export interface ReviewResult {
  findings: ReviewFinding[];
  specialists: Record<string, { label: string; status: "ok" | "error"; findings: number; summary: string; error?: string }>;
}

// ---- AI suggestions (LLM follow-ups and starting prompts) ----
/** kind: "start" (empty conversation), "follow_up" (after an answer), "reply" (an answer to the AI's question). */
export interface AiSuggestion { label: string; prompt: string; why?: string; kind?: "start" | "follow_up" | "reply" }
export type SuggestionMode = "edit" | "ask" | "design";

// ---- AI observability: agent traces and the prompt catalog ----
export interface AiSpan {
  kind: "model" | "tool" | "api"; name: string; started_at: string; latency_ms: number; status: "ok" | "error" | "running";
  input: unknown; output: unknown; error: string; model: string;
  prompt_tokens: number; completion_tokens: number; cost_usd: number; calls: number;
}
export interface AiTraceSummary {
  id: string; org_id: string; project_id: string; project_name?: string; feature: string; prompt: string; agent: string;
  /** Who asked, what they asked and what the AI answered (short previews). */
  user_id: string; user_name: string; question: string; answer: string;
  /** The run that fired this one (agents run in parallel); "" for a top-level run. */
  parent_id: string;
  status: "ok" | "error"; error: string; prompt_tokens: number; completion_tokens: number; cost_usd: number;
  latency_ms: number; model_calls: number; tool_calls: number; models: string[]; created_at: string;
}
export interface AiTrace extends AiTraceSummary {
  input: unknown; output: unknown; spans: AiSpan[]; metadata: Record<string, unknown>;
  /** Agents this run fired in parallel. */
  children: AiTraceSummary[];
}
export interface AiTraceList {
  traces: AiTraceSummary[]; people: { id: string; name: string }[]; content: "full" | "metadata" | "off"; retention_days: number;
}
export interface PromptInfo {
  id: string; feature: string; description: string; active: string; default: string;
  versions: { version: string; date: string; notes: string; sha: string }[];
}

// ---- comments and notifications ----
export type CommentTarget = "project" | "node" | "decision";
export interface Comment {
  id: string; project_id: string; branch: string; target_type: CommentTarget; target_id: string; parent_id: string;
  author_id: string; author_name: string; body: string; mentions: string[]; resolved: boolean; resolved_by: string;
  created_at: string; edited_at: string | null;
}
export interface AppNotification {
  id: number; project_id: string; project_name: string; branch: string; comment_id: string; thread_id: string;
  kind: "mention" | "reply"; actor_name: string; preview: string; read: boolean; created_at: string;
}
