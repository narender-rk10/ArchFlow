import { Handle, Position, type NodeProps, type Node } from "@xyflow/react";
import { memo } from "react";
import type { ArchNode as ArchNodeModel, NodeType } from "../types";
import { VENDOR_STYLE } from "./Palette";

export const TYPE_META: Record<NodeType, { label: string; color: string; glyph: string }> = {
  client: { label: "Client", color: "#0090ff", glyph: "▭" },
  gateway: { label: "Gateway", color: "#6e56cf", glyph: "⇶" },
  service: { label: "Service", color: "#30a46c", glyph: "◆" },
  database: { label: "Database", color: "#f76b15", glyph: "⛁" },
  cache: { label: "Cache", color: "#e5484d", glyph: "⚡" },
  queue: { label: "Queue", color: "#ffb224", glyph: "☰" },
  storage: { label: "Storage", color: "#ad7f58", glyph: "▤" },
  agent: { label: "AI Agent", color: "#d6409f", glyph: "✦" },
  llm: { label: "LLM", color: "#8e4ec6", glyph: "◎" },
  mcp: { label: "MCP", color: "#12a594", glyph: "⌘" },
  external: { label: "External", color: "#8b8d98", glyph: "↗" },
  security: { label: "Security", color: "#dc3e42", glyph: "⛨" },
  note: { label: "Note", color: "#ffc53d", glyph: "✎" },
};

export type DiffMark = "added" | "removed" | "changed" | null;

export interface ArchNodeData extends Record<string, unknown> {
  model: ArchNodeModel;
  dimmed?: boolean;
  highlighted?: boolean;
  diff?: DiffMark;
  remote?: { name: string; color: string }[];
  issues?: number;
}

export type ArchFlowNode = Node<ArchNodeData, "arch">;

function ArchNodeView({ data, selected }: NodeProps<ArchFlowNode>) {
  const { model, dimmed, highlighted, diff, remote, issues } = data;
  const meta = TYPE_META[model.type] ?? TYPE_META.service;
  const ring = remote?.[0]?.color;
  const cls = ["arch-node", `t-${model.type}`, selected && "selected", dimmed && "dimmed", highlighted && "highlighted",
    diff && `diff-${diff}`].filter(Boolean).join(" ");

  if (model.type === "note") {
    return (
      <div className={cls} style={ring ? { boxShadow: `0 0 0 2px ${ring}` } : undefined}>
        <div className="note-text">{model.description || model.name}</div>
      </div>
    );
  }

  return (
    <div className={cls} style={{ ["--accent" as string]: meta.color, ...(ring ? { boxShadow: `0 0 0 2px ${ring}` } : {}) }}>
      <Handle type="target" position={Position.Top} />
      <Handle type="target" position={Position.Left} id="l" />
      <div className="an-head">
        <span className="an-glyph" aria-hidden>{meta.glyph}</span>
        <span className="an-type">{meta.label}</span>
        {model.vendor && VENDOR_STYLE[model.vendor] && (
          <span className="vbadge" style={{ ["--vc" as string]: VENDOR_STYLE[model.vendor].color }}>{VENDOR_STYLE[model.vendor].short}</span>
        )}
        {!!issues && <span className="an-issues" title={`${issues} issue(s)`}>{issues}</span>}
      </div>
      <div className="an-name">{model.name}</div>
      {model.technology && <div className="an-tech">{model.technology}</div>}
      {model.requirement_ids.length > 0 && (
        <div className="an-reqs">
          {model.requirement_ids.slice(0, 4).map((r) => <span key={r} className="chip">{r}</span>)}
          {model.requirement_ids.length > 4 && <span className="chip">+{model.requirement_ids.length - 4}</span>}
        </div>
      )}
      {remote && remote.length > 0 && (
        <div className="an-remote" style={{ background: remote[0].color }}>{remote.map((r) => r.name).join(", ")}</div>
      )}
      <Handle type="source" position={Position.Bottom} />
      <Handle type="source" position={Position.Right} id="r" />
    </div>
  );
}

export const ArchNodeComponent = memo(ArchNodeView);
