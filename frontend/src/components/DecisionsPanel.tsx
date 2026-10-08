import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { can, type Branch, type Decision, type DecisionAlternative, type DecisionStatus, type Evaluation, type Role, type User } from "../types";

interface Props {
  pid: string;
  branches: Branch[];
  user: User;
  role?: Role | null;
  onAdopted: (r: { version: number; branch: string; source: string; decisionId: string }) => void;
  toast: (text: string) => void;
}

const STATUSES: DecisionStatus[] = ["proposed", "accepted", "rejected", "superseded"];
const ENGINE_LABEL: Record<Evaluation["engine"], string> = {
  llm: "LLM evaluation", heuristic: "Offline heuristic",
};
const TARGET = "main";

function label(ev: Evaluation, c: string) {
  return ev.criteria_labels?.[c] ?? c.replace(/_/g, " ");
}

/** Criteria × alternatives score table; the best value in each row is highlighted. */
export function ScoreMatrix({ ev }: { ev: Evaluation }) {
  const alts = ev.alternatives;
  return (
    <div className="adr-matrix-wrap">
      <table className="adr-matrix">
        <thead>
          <tr>
            <th>Criterion</th>
            {alts.map((a) => (
              <th key={a} className={a === ev.recommended ? "rec" : ""} title={a}>
                {a === ev.recommended && <span aria-label="recommended">★ </span>}{a}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {ev.criteria.map((c) => {
            const vals = alts.map((a) => ev.scores[a]?.[c]).filter((v): v is number => typeof v === "number");
            const best = vals.length ? Math.max(...vals) : null;
            return (
              <tr key={c}>
                <td>{label(ev, c)}</td>
                {alts.map((a) => {
                  const v = ev.scores[a]?.[c];
                  return <td key={a} className={v !== undefined && v === best && vals.length > 1 ? "best" : ""}>{v ?? "–"}</td>;
                })}
              </tr>
            );
          })}
        </tbody>
        <tfoot>
          <tr>
            <td>Weighted avg</td>
            {alts.map((a) => <td key={a} className={a === ev.recommended ? "best" : ""}>{ev.totals?.[a] ?? "–"}</td>)}
          </tr>
        </tfoot>
      </table>
      <div className="row between small muted">
        <span className={`adr-engine e-${ev.engine}`}>{ENGINE_LABEL[ev.engine] ?? ev.engine}</span>
        <span>1–5, higher is better{ev.confidence != null ? ` · confidence ${Math.round(ev.confidence * 100)}%` : ""}</span>
      </div>
    </div>
  );
}

/** Alternatives for a decision record, derived from an evaluation (strong criteria = pros, weak = cons). */
function alternativesFrom(ev: Evaluation): DecisionAlternative[] {
  return ev.alternatives.map((b) => {
    const s = ev.scores[b] ?? {};
    return {
      name: b, branch: b, summary: `Weighted score ${ev.totals?.[b] ?? "–"} / 5 (${ev.engine})`,
      pros: Object.entries(s).filter(([, v]) => v >= 4).map(([c, v]) => `${label(ev, c)} ${v}/5`),
      cons: Object.entries(s).filter(([, v]) => v <= 2).map(([c, v]) => `${label(ev, c)} ${v}/5`),
    };
  });
}

export function DecisionsPanel({ pid, branches, user, role, onAdopted, toast }: Props) {
  const canEdit = role === undefined || can(role, "edit");
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [openId, setOpenId] = useState<string | null>(null);
  const [comparing, setComparing] = useState(false);
  const [picked, setPicked] = useState<string[]>([]);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<Evaluation | null>(null);
  const [error, setError] = useState("");

  const reload = useCallback(() => {
    api.decisions(pid).then(setDecisions).catch((e) => setError((e as Error).message));
  }, [pid]);
  useEffect(reload, [reload]);

  const toggle = (name: string) =>
    setPicked((p) => (p.includes(name) ? p.filter((x) => x !== name) : p.length >= 4 ? p : [...p, name]));

  const run = async () => {
    setRunning(true); setError(""); setResult(null);
    try { setResult(await api.evaluate(pid, picked, user.name)); }
    catch (e) { setError((e as Error).message); }
    finally { setRunning(false); }
  };

  const record = async () => {
    if (!result) return;
    try {
      const d = await api.createDecision(pid, {
        title: `Choose an architecture: ${result.alternatives.join(" vs ")}`,
        status: "proposed",
        context: `${result.alternatives.length} candidate architectures were compared against the project's requirements. Evaluation engine: ${ENGINE_LABEL[result.engine] ?? result.engine}.`,
        decision: `Adopt the architecture in branch '${result.recommended}'. ${result.rationale}`.trim(),
        consequences: "",
        alternatives: alternativesFrom(result),
        chosen: result.recommended,
        requirement_ids: result.requirement_ids ?? [],
        evaluation: { ...result, raw: undefined },
        actor: user.name,
      });
      toast(`Recorded ${d.id}`);
      setComparing(false); setResult(null); setPicked([]);
      reload();
      setOpenId(d.id);
    } catch (e) { toast(`⚠ ${(e as Error).message}`); }
  };

  const open = decisions.find((d) => d.id === openId);
  if (open) {
    return (
      <DecisionDetail key={open.id + open.updated_at} pid={pid} decision={open} branches={branches} user={user} role={role}
        toast={toast} onBack={() => setOpenId(null)}
        onSaved={(d) => setDecisions((ds) => ds.map((x) => (x.id === d.id ? d : x)))}
        onAdopted={(r) => { reload(); onAdopted(r); }} />
    );
  }

  return (
    <div className="panel">
      <div className="panel-tools">
        <span className="grow muted small">Architecture decision records</span>
        {canEdit && <button className={comparing ? "" : "primary"} onClick={() => { setComparing(!comparing); setResult(null); setError(""); }}>
          {comparing ? "Close" : "Compare drafts"}
        </button>}
      </div>

      {comparing && (
        <div className="adr-compare">
          <div className="small muted">Pick 2–4 branches to score against the requirements.</div>
          <div className="adr-branches">
            {branches.map((b) => (
              <label key={b.name} className="row gap small">
                <input type="checkbox" checked={picked.includes(b.name)} onChange={() => toggle(b.name)}
                  disabled={!picked.includes(b.name) && picked.length >= 4} />
                <span className="mono">{b.name}</span>
              </label>
            ))}
            {branches.length < 2 && <p className="small warn">Create at least two branches (drafts) to compare, e.g. from the design chat.</p>}
          </div>
          <div className="row gap">
            <button className="primary" disabled={running || picked.length < 2} onClick={run}>
              {running ? "Evaluating…" : `Evaluate ${picked.length || ""}`.trim()}
            </button>
          </div>
          {error && <p className="small error">{error}</p>}
          {result && (
            <div className="adr-result">
              <ScoreMatrix ev={result} />
              <p className="small"><b>Recommended: {result.recommended}.</b> {result.rationale}</p>
              {result.notes.length > 0 && <p className="tiny muted">{result.notes.join(" · ")}</p>}
              <button onClick={record}>Create decision record</button>
            </div>
          )}
        </div>
      )}

      <h4 className="pad">Decisions</h4>
      {decisions.length === 0 && <p className="muted small pad">No decisions yet. Compare drafts to record your first ADR.</p>}
      <ul className="adr-list">
        {decisions.map((d) => (
          <li key={d.id} onClick={() => setOpenId(d.id)}>
            <div className="row between">
              <span className="mono small">{d.id}</span>
              <span className={`status adr-s-${d.status}`}>{d.status}</span>
            </div>
            <div className="adr-title">{d.title}</div>
            <div className="muted small">
              {d.chosen ? <>chosen <span className="mono">{d.chosen}</span> · </> : null}by {d.author}
              {d.evaluation ? ` · ${d.evaluation.engine}` : ""}
            </div>
          </li>
        ))}
      </ul>
      {!comparing && error && <p className="small error pad">{error}</p>}
    </div>
  );
}

interface DetailProps {
  pid: string;
  decision: Decision;
  branches: Branch[];
  user: User;
  role?: Role | null;
  toast: (text: string) => void;
  onBack: () => void;
  onSaved: (d: Decision) => void;
  onAdopted: (r: { version: number; branch: string; source: string; decisionId: string }) => void;
}

function DecisionDetail({ pid, decision: d, branches, user, role, toast, onBack, onSaved, onAdopted }: DetailProps) {
  const canEdit = role === undefined || can(role, "edit");
  const canReview = role === undefined || can(role, "review"); // reviewers may change the status only
  const [form, setForm] = useState({ title: d.title, status: d.status, context: d.context, decision: d.decision, consequences: d.consequences });
  const [saving, setSaving] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [adopting, setAdopting] = useState(false);
  const dirty = (Object.keys(form) as (keyof typeof form)[]).some((k) => form[k] !== d[k]);

  const names = new Set(branches.map((b) => b.name));
  const alt = d.alternatives.find((a) => a.name === d.chosen || a.branch === d.chosen);
  const source = alt?.branch && names.has(alt.branch) ? alt.branch : names.has(d.chosen) ? d.chosen : null;
  const canAdopt = canEdit && !!source && source !== TARGET && (d.status === "proposed" || d.status === "accepted");

  const save = async () => {
    const changes = Object.fromEntries((Object.keys(form) as (keyof typeof form)[])
      .filter((k) => form[k] !== d[k]).map((k) => [k, form[k]]));
    setSaving(true);
    try {
      const out = await api.updateDecision(pid, d.id, { ...changes, actor: user.name });
      onSaved(out);
      toast(`Saved ${d.id}`);
    } catch (e) { toast(`⚠ ${(e as Error).message}`); }
    finally { setSaving(false); }
  };

  const adopt = async () => {
    if (!source) return;
    setAdopting(true);
    try {
      const r = await api.adoptDecision(pid, d.id, TARGET, user.name);
      onSaved(r.decision);
      onAdopted({ version: r.version, branch: r.branch, source: r.source_branch, decisionId: d.id });
    } catch (e) { toast(`⚠ ${(e as Error).message}`); }
    finally { setAdopting(false); setConfirm(false); }
  };

  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  return (
    <div className="panel adr-detail">
      <div className="panel-tools">
        <button className="small" onClick={onBack}>← All decisions</button>
        <span className="mono small grow">{d.id}</span>
        <span className={`status adr-s-${d.status}`}>{d.status}</span>
      </div>
      <div className="adr-form">
        <label className="field"><span>Title</span><input value={form.title} onChange={set("title")} disabled={!canEdit} /></label>
        <label className="field"><span>Status</span>
          <select value={form.status} onChange={set("status")} disabled={!canReview}>
            {STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        <label className="field"><span>Context</span><textarea rows={3} value={form.context} onChange={set("context")} disabled={!canEdit} /></label>
        <label className="field"><span>Decision</span><textarea rows={3} value={form.decision} onChange={set("decision")} disabled={!canEdit} /></label>
        <label className="field"><span>Consequences</span><textarea rows={3} value={form.consequences} onChange={set("consequences")} disabled={!canEdit} /></label>
        {canReview && <div className="row gap">
          <button className="primary" disabled={!dirty || saving || !form.title.trim()} onClick={save}>{saving ? "Saving…" : "Save"}</button>
          {dirty && <button onClick={() => setForm({ title: d.title, status: d.status, context: d.context, decision: d.decision, consequences: d.consequences })}>Discard</button>}
        </div>}
      </div>

      {d.alternatives.length > 0 && (
        <>
          <h4 className="pad">Alternatives</h4>
          <ul className="adr-alts">
            {d.alternatives.map((a) => (
              <li key={a.name} className={a === alt ? "chosen" : ""}>
                <div className="row between">
                  <b>{a.name}</b>
                  {a === alt && <span className="chip on">chosen</span>}
                </div>
                {a.branch && a.branch !== a.name && <div className="small muted mono">⎇ {a.branch}</div>}
                {a.summary && <div className="small">{a.summary}</div>}
                {(a.pros.length > 0 || a.cons.length > 0) && (
                  <div className="pc small">
                    <ul className="pros">{a.pros.map((p, i) => <li key={i}>{p}</li>)}</ul>
                    <ul className="cons">{a.cons.map((c, i) => <li key={i}>{c}</li>)}</ul>
                  </div>
                )}
              </li>
            ))}
          </ul>
        </>
      )}

      {d.evaluation && (
        <>
          <h4 className="pad">Evaluation</h4>
          <div className="pad"><ScoreMatrix ev={d.evaluation} /></div>
        </>
      )}

      {d.requirement_ids.length > 0 && (
        <div className="pad small muted adr-reqs">Requirements: {d.requirement_ids.map((r) => <span key={r} className="chip">{r}</span>)}</div>
      )}

      {canAdopt && (
        <div className="adr-adopt">
          {confirm ? (
            <>
              <p className="small">Replace <b>{TARGET}</b> with <b className="mono">{source}</b>? A safety version of {TARGET} is committed first, so you can restore it.</p>
              <div className="row gap">
                <button className="danger" disabled={adopting} onClick={adopt}>{adopting ? "Adopting…" : "Confirm adopt"}</button>
                <button onClick={() => setConfirm(false)}>Cancel</button>
              </div>
            </>
          ) : (
            <button className="primary" onClick={() => setConfirm(true)}>Adopt into {TARGET}</button>
          )}
        </div>
      )}
      <p className="tiny muted pad">by {d.author} · created {new Date(d.created_at).toLocaleString()}</p>
    </div>
  );
}
