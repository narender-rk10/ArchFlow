export const NODE_TYPES = [
  "client", "gateway", "service", "database", "cache", "queue", "storage",
  "agent", "llm", "mcp", "external", "security", "note",
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
export interface Org { id: string; name: string; created_at: string }
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
  };
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
