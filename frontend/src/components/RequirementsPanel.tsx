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
type View = "all" | "untraced" | "issues" | "nfr";

export function RequirementsPanel({ state, issues, traced, onTrace, send, onPrompt, readOnly = false }: Props) {
  const [open, setOpen] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [view, setView] = useState<View>("all");
  const all = Object.values(state.requirements);
  const linked = (id: string) => Object.values(state.nodes).filter((n) => n.requirement_ids.includes(id));
  const issuesOf = (id: string) => issues.filter((i) => i.requirement_ids.includes(id));
  const untraced = all.filter((r) => linked(r.id).length === 0);
  const withIssues = all.filter((r) => issuesOf(r.id).length > 0);
  const views: { id: View; label: string; count: number }[] = [
    { id: "all", label: "All", count: all.length },
    { id: "untraced", label: "Not traced", count: untraced.length },
    { id: "issues", label: "Has issues", count: withIssues.length },
    { id: "nfr", label: "NFRs", count: all.filter((r) => r.kind === "non_functional").length },
  ];
  const reqs = all.filter((r) =>
    (view === "all" || (view === "untraced" && untraced.includes(r)) || (view === "issues" && withIssues.includes(r))
      || (view === "nfr" && r.kind === "non_functional"))
    && (!filter || `${r.id} ${r.title} ${r.description}`.toLowerCase().includes(filter.toLowerCase())));

  const add = (kind: Requirement["kind"]) =>
    send([{ op: "add_requirement", requirement: { title: kind === "functional" ? "New requirement" : "New non-functional requirement", kind } }]);

  return (
    <div className="panel">
      <div className="panel-head">
        <div className="panel-summary small">
          {all.length === 0 ? "No requirements yet"
            : <><b>{all.length}</b> requirement{all.length === 1 ? "" : "s"}
              {untraced.length > 0 && <> · <span className="warn">{untraced.length} not traced</span></>}
              {withIssues.length > 0 && <> · <span className="warn">{withIssues.length} with issues</span></>}</>}
        </div>
        {!readOnly && (
          <div className="row gap">
            <button className="small primary" onClick={() => add("functional")} title="A feature the system must provide">+ Functional</button>
            <button className="small" onClick={() => add("non_functional")} title="A quality target: performance, security, cost…">+ Non-functional</button>
          </div>
        )}
      </div>
      {all.length > 0 && (
        <div className="panel-filters">
          <div className="seg-chips" role="tablist" aria-label="Show">
            {views.map((v) => (
              <button key={v.id} role="tab" aria-selected={view === v.id} className={view === v.id ? "on" : ""}
                onClick={() => setView(v.id)} disabled={v.count === 0 && v.id !== "all"}>{v.label} <span className="muted">{v.count}</span></button>
            ))}
          </div>
          {all.length > 6 && <input type="search" placeholder="Search requirements…" value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Search requirements" />}
        </div>
      )}
      {all.length === 0 && (
        <div className="empty-state">
          <p className="small">Requirements are what the system must do and how well. Add them here, or describe your idea in the
            assistant's <b>Design</b> mode and it will write them with you.</p>
        </div>
      )}
      {all.length > 0 && reqs.length === 0 && <p className="muted small pad">Nothing matches.</p>}
      <ul className="req-list">
        {reqs.map((r) => {
          const mine = issuesOf(r.id);
          const nodes = linked(r.id);
          const isOpen = open === r.id;
          const upd = (field: string, value: unknown, base_rev: number) =>
            send([{ op: "update_requirement", id: r.id, changes: { [field]: value }, base_rev }]);
          return (
            <li key={r.id} className={`req ${traced === r.id ? "traced" : ""} ${isOpen ? "open" : ""}`}>
              <button className="req-row" aria-expanded={isOpen}
                onClick={() => { setOpen(isOpen ? null : r.id); onTrace(traced === r.id ? null : r.id); }}
                title={nodes.length ? `Implemented by ${nodes.map((n) => n.name).join(", ")}` : "No component implements this yet"}>
                <span className={`req-dot ${mine.length ? "bad" : nodes.length ? "ok" : "warn"}`} aria-hidden />
                <span className="req-title">{r.title}</span>
                <span className={`status s-${r.status}`}>{r.status}</span>
              </button>
              <div className="req-meta small muted">
                <span className="mono">{r.id}</span> · {r.kind === "non_functional" ? `NFR${r.category ? ` · ${r.category}` : ""}` : "Functional"} · {r.priority}
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
                    <button onClick={() => onPrompt(r.id)} title="Turn this requirement into a change spec for a coding agent">⚙ Vibe code this</button>
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
