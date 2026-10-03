import { NODE_TYPES, type Catalog, type Op, type ProjectState } from "../types";
import { VENDOR_STYLE } from "./Palette";
import { Field } from "./Field";
import { TYPE_META } from "./ArchNode";
import { LldViews } from "./LldViews";

interface Props {
  catalog: Catalog | null;
  state: ProjectState;
  nodeIds: string[];
  edgeIds: string[];
  send: (ops: Op[]) => void;
  onPrompt: () => void;
  // HLD → LLD drill-down (optional so the inspector also works without it)
  pid?: string;
  branch?: string;
  onOpenInternals?: (nodeId: string) => void;
}

export function Inspector({ catalog, state, nodeIds, edgeIds, send, onPrompt, pid, branch, onOpenInternals }: Props) {
  if (nodeIds.length === 1 && edgeIds.length === 0) {
    const n = state.nodes[nodeIds[0]];
    if (!n) return null;
    const upd = (field: string, value: unknown, base_rev: number) =>
      send([{ op: "update_node", id: n.id, changes: { [field]: value }, base_rev }]);
    const reqs = Object.values(state.requirements);
    return (
      <div className="inspector">
        <div className="insp-head" style={{ ["--accent" as string]: TYPE_META[n.type].color }}>
          <span className="an-glyph">{TYPE_META[n.type].glyph}</span> {n.name}
          <span className="muted small mono">{n.id}</span>
        </div>
        <Field label="Name" value={n.name} rev={state.rev} onCommit={(v, b) => upd("name", v, b)} />
        <Field label="Type" value={n.type} rev={state.rev} options={NODE_TYPES} onCommit={(v, b) => upd("type", v, b)} />
        {(() => {
          const entry = catalog?.items.find((c) => c.id === n.catalog_id);
          const alts = entry?.kind ? catalog!.items.filter((c) => c.kind === entry.kind && c.id !== entry.id) : [];
          if (!entry) return null;
          return (
            <label className="field">
              <span>Service · {entry.kind_label || entry.category}
                {n.vendor && VENDOR_STYLE[n.vendor] && <span className="vbadge" style={{ ["--vc" as string]: VENDOR_STYLE[n.vendor].color, marginLeft: 6 }}>{VENDOR_STYLE[n.vendor].short}</span>}
              </span>
              {alts.length > 0 && (
                <select value="" aria-label="Swap for an equivalent service" onChange={(e) => {
                  const alt = catalog!.items.find((c) => c.id === e.target.value);
                  if (!alt) return;
                  const changes: Record<string, unknown> = { technology: alt.name, vendor: alt.vendor, catalog_id: alt.id };
                  if (n.name === entry.name || n.name === n.technology) changes.name = alt.name;
                  send([{ op: "update_node", id: n.id, changes }]);
                }}>
                  <option value="">Swap for an equivalent…</option>
                  {alts.map((a) => <option key={a.id} value={a.id}>{a.name} ({catalog!.vendors[a.vendor]})</option>)}
                </select>
              )}
            </label>
          );
        })()}
        <Field label="Technology" value={n.technology} rev={state.rev} placeholder="e.g. FastAPI, PostgreSQL"
          onCommit={(v, b) => upd("technology", v, b)} />
        <Field label="Description" value={n.description} rev={state.rev} multiline onCommit={(v, b) => upd("description", v, b)} />
        {n.type === "endpoint" && (<>
          <Field label="HTTP method" value={n.props?.method ?? "GET"} rev={state.rev} options={["GET", "POST", "PUT", "PATCH", "DELETE"]}
            onCommit={(v, b) => send([{ op: "update_node", id: n.id, changes: { props: { ...n.props, method: v }, name: `${v} ${n.props?.path ?? "/"}` }, base_rev: b }])} />
          <Field label="Path" value={n.props?.path ?? ""} rev={state.rev} placeholder="/orders/{id}"
            onCommit={(v, b) => send([{ op: "update_node", id: n.id, changes: { props: { ...n.props, path: v }, name: `${n.props?.method ?? "GET"} ${v}` }, base_rev: b }])} />
        </>)}
        {n.type === "table" && (
          <Field label="Columns (name type [pk|fk], comma-separated)" value={n.props?.columns ?? ""} rev={state.rev} multiline
            placeholder="id uuid pk, customer_id uuid fk, total numeric"
            onCommit={(v, b) => upd("props", { ...n.props, columns: v }, b)} />
        )}
        {n.type === "event" && (
          <Field label="Channel / topic" value={n.props?.channel ?? ""} rev={state.rev} onCommit={(v, b) => upd("props", { ...n.props, channel: v }, b)} />
        )}
        <div className="field">
          <span>Implements requirements</span>
          <div className="req-links">
            {reqs.length === 0 && <span className="muted small">No requirements yet.</span>}
            {reqs.map((r) => {
              const on = n.requirement_ids.includes(r.id);
              return (
                <button key={r.id} className={`chip toggle ${on ? "on" : ""}`} title={r.title}
                  onClick={() => upd("requirement_ids", on ? n.requirement_ids.filter((x) => x !== r.id) : [...n.requirement_ids, r.id], state.rev)}>
                  {r.id}
                </button>
              );
            })}
          </div>
        </div>
        <div className="insp-actions">
          {onOpenInternals && n.type !== "note" && (
            <button onClick={() => onOpenInternals(n.id)} title="Open this component's internal (low-level) diagram">
              Open internals ↘{(() => { const k = Object.values(state.nodes).filter((x) => x.parent_id === n.id).length; return k ? ` (${k})` : ""; })()}
            </button>
          )}
          <button onClick={onPrompt}>⚙ Build this (coding prompt)</button>
          <button className="danger" onClick={() => send([{ op: "remove_node", id: n.id }])}>Delete</button>
        </div>
        {pid && branch && !n.parent_id && n.type !== "note" && <LldViews pid={pid} branch={branch} nodeId={n.id} rev={state.rev} />}
        <p className="muted small">rev {n.rev} · connections {Object.values(state.edges).filter((e) => e.source === n.id || e.target === n.id).length}</p>
      </div>
    );
  }

  if (edgeIds.length === 1 && nodeIds.length === 0) {
    const e = state.edges[edgeIds[0]];
    if (!e) return null;
    const upd = (field: string, value: unknown, base_rev: number) =>
      send([{ op: "update_edge", id: e.id, changes: { [field]: value }, base_rev }]);
    return (
      <div className="inspector">
        <div className="insp-head">Connection</div>
        <p className="small">{state.nodes[e.source]?.name} → {state.nodes[e.target]?.name}</p>
        <Field label="Protocol" value={e.protocol} rev={state.rev} placeholder="REST, gRPC, Kafka, MCP, A2A…" onCommit={(v, b) => upd("protocol", v, b)} />
        <Field label="Label" value={e.label} rev={state.rev} onCommit={(v, b) => upd("label", v, b)} />
        <div className="insp-actions">
          <button onClick={() => send([{ op: "update_edge", id: e.id, changes: { source: e.target, target: e.source } }])}>⇄ Reverse</button>
          <button className="danger" onClick={() => send([{ op: "remove_edge", id: e.id }])}>Delete</button>
        </div>
      </div>
    );
  }

  if (nodeIds.length > 1 || edgeIds.length > 1) {
    return (
      <div className="inspector">
        <div className="insp-head">{nodeIds.length} components, {edgeIds.length} connections</div>
        <p className="muted small">Ctrl+C / Ctrl+V to copy, Ctrl+D to duplicate, Delete to remove.</p>
        <div className="insp-actions">
          <button onClick={onPrompt}>⚙ Build selected architecture</button>
          <button className="danger" onClick={() => send([
            ...edgeIds.filter((id) => !nodeIds.includes(state.edges[id]?.source) && !nodeIds.includes(state.edges[id]?.target))
              .map((id) => ({ op: "remove_edge", id })),
            ...nodeIds.map((id) => ({ op: "remove_node", id })),
          ])}>Delete selection</button>
        </div>
      </div>
    );
  }

  return (
    <div className="inspector empty">
      <p className="muted small">Select a component or connection to edit it.</p>
      <ul className="muted small tips">
        <li>Drag components from the palette onto the canvas.</li>
        <li>Drag from a handle to another component to connect.</li>
        <li>Shift-drag to box-select; Delete removes.</li>
        <li>Type commands like “Add Redis between Order Service and Orders DB”.</li>
      </ul>
    </div>
  );
}
