import { useState } from "react";
import type { Issue, Op, ProjectState, Requirement } from "../types";
import { Field } from "./Field";

interface Props {
  state: ProjectState;
  issues: Issue[];
  traced: string | null;
  onTrace: (id: string | null) => void;
  send: (ops: Op[]) => void;
  onPrompt: (reqId: string) => void;
  readOnly?: boolean;
}

const splitList = (s: string, sep: string | RegExp) => s.split(sep).map((x) => x.trim()).filter(Boolean);
const STATUS = ["draft", "review", "approved", "implemented"] as const;
const PRIORITY = ["low", "medium", "high", "critical"] as const;

export function RequirementsPanel({ state, issues, traced, onTrace, send, onPrompt, readOnly = false }: Props) {
  const [open, setOpen] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const reqs = Object.values(state.requirements).filter((r) =>
    !filter || `${r.id} ${r.title} ${r.description}`.toLowerCase().includes(filter.toLowerCase()));
  const linked = (id: string) => Object.values(state.nodes).filter((n) => n.requirement_ids.includes(id));

  const add = (kind: Requirement["kind"]) =>
    send([{ op: "add_requirement", requirement: { title: kind === "functional" ? "New requirement" : "New non-functional requirement", kind } }]);

  return (
    <div className="panel">
      <div className="panel-tools">
        <input placeholder="Filter requirements…" value={filter} onChange={(e) => setFilter(e.target.value)} />
        {!readOnly && <button onClick={() => add("functional")} title="Add functional requirement">+ FR</button>}
        {!readOnly && <button onClick={() => add("non_functional")} title="Add non-functional requirement">+ NFR</button>}
      </div>
      {reqs.length === 0 && <p className="muted small pad">No requirements. Add one, or describe your idea in the command bar and let the AI interview you.</p>}
      <ul className="req-list">
        {reqs.map((r) => {
          const mine = issues.filter((i) => i.requirement_ids.includes(r.id));
          const nodes = linked(r.id);
          const isOpen = open === r.id;
          const upd = (field: string, value: unknown, base_rev: number) =>
            send([{ op: "update_requirement", id: r.id, changes: { [field]: value }, base_rev }]);
          return (
            <li key={r.id} className={`req ${traced === r.id ? "traced" : ""}`}>
              <div className="req-row" onClick={() => { setOpen(isOpen ? null : r.id); onTrace(traced === r.id ? null : r.id); }}>
                <span className="mono small">{r.id}</span>
                <span className="req-title">{r.title}</span>
                <span className={`status s-${r.status}`}>{r.status}</span>
              </div>
              <div className="req-meta small muted">
                {r.kind === "non_functional" ? `NFR${r.category ? " · " + r.category : ""}` : "Functional"} · {r.priority}
                {" · "}{nodes.length ? `${nodes.length} component${nodes.length > 1 ? "s" : ""}` : <span className="warn">not traced</span>}
                {mine.length > 0 && <span className="warn"> · {mine.length} issue{mine.length > 1 ? "s" : ""}</span>}
              </div>
              {isOpen && (
                <div className="req-edit">
                  {mine.map((i, k) => (
                    <div key={k} className={`issue ${i.severity}`}><b>{i.message}</b> {i.suggestion}</div>
                  ))}
                  <fieldset className="bare" disabled={readOnly}>
                  <Field label="Title" value={r.title} rev={state.rev} onCommit={(v, b) => upd("title", v, b)} />
                  <Field label="Description" value={r.description} rev={state.rev} multiline onCommit={(v, b) => upd("description", v, b)} />
                  <div className="grid2">
                    <Field label="Status" value={r.status} rev={state.rev} options={STATUS} onCommit={(v, b) => upd("status", v, b)} />
                    <Field label="Priority" value={r.priority} rev={state.rev} options={PRIORITY} onCommit={(v, b) => upd("priority", v, b)} />
                    <Field label="Kind" value={r.kind} rev={state.rev} options={["functional", "non_functional"]} onCommit={(v, b) => upd("kind", v, b)} />
                    <Field label="Category" value={r.category} rev={state.rev} placeholder="performance, security…" onCommit={(v, b) => upd("category", v, b)} />
                  </div>
                  <Field label="Actors (comma-separated)" value={r.actors.join(", ")} rev={state.rev}
                    onCommit={(v, b) => upd("actors", splitList(v, ","), b)} />
                  <Field label="Acceptance criteria (one per line, Ctrl+Enter to save)" value={r.acceptance_criteria.join("\n")} rev={state.rev} multiline
                    onCommit={(v, b) => upd("acceptance_criteria", splitList(v, /\n/), b)} />
                  </fieldset>
                  <div className="insp-actions">
                    <button onClick={() => onPrompt(r.id)}>⚙ Generate implementation</button>
                    {!readOnly && <button className="danger" onClick={() => send([{ op: "remove_requirement", id: r.id }])}>Delete</button>}
                  </div>
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
