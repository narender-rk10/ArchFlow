import { useEffect, useState } from "react";
import { api } from "../api";
import type { JiraPlan, JiraPlanItem, JiraPushResult } from "../types";
import { Modal } from "./OrgDialogs";

interface Props {
  pid: string;
  branch: string;
  defaultKey: string;
  aiAvailable: boolean;
  onClose: () => void;
  /** After a push or a status refresh: reload the links shown next to requirements and components. */
  onChanged: () => void;
}

const TYPE_LABEL: Record<JiraPlanItem["type"], string> = { epic: "Epic", story: "Story", subtask: "Sub-task", link: "Link" };

/**
 * Push this branch to Jira: always a dry-run preview first (what will be created, updated or left alone), then an
 * explicit push. Re-pushing updates the same issues; failed items are listed and the next push retries only them.
 */
export function JiraDialog({ pid, branch, defaultKey, aiAvailable, onClose, onChanged }: Props) {
  const [key, setKey] = useState(defaultKey);
  const [useAi, setUseAi] = useState(aiAvailable);
  const [plan, setPlan] = useState<JiraPlan | null>(null);
  const [result, setResult] = useState<JiraPushResult | null>(null);
  const [busy, setBusy] = useState<"" | "plan" | "push" | "refresh">("");
  const [open, setOpen] = useState<string | null>(null);
  const [error, setError] = useState("");

  const preview = async () => {
    setBusy("plan"); setError(""); setResult(null);
    try { setPlan(await api.jiraPlan(pid, branch, key, useAi)); } catch (e) { setError((e as Error).message); }
    setBusy("");
  };
  useEffect(() => { if (key) preview(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const push = async () => {
    if (!plan) return;
    const n = plan.counts.create + plan.counts.update;
    if (!n || !window.confirm(`Push to Jira project ${plan.project_key}? ${plan.counts.create} to create, ${plan.counts.update} to update.`)) return;
    setBusy("push"); setError("");
    try { setResult(await api.jiraPush(pid, branch, key, useAi)); onChanged(); setPlan(await api.jiraPlan(pid, branch, key, useAi)); }
    catch (e) { setError((e as Error).message); }
    setBusy("");
  };
  const refresh = async () => {
    setBusy("refresh"); setError("");
    try { const r = await api.jiraRefresh(pid, branch); onChanged(); setError(""); window.alert(`Updated the status of ${r.updated} issue${r.updated === 1 ? "" : "s"}.`); }
    catch (e) { setError((e as Error).message); }
    setBusy("");
  };

  const failed = result?.results.filter((r) => r.status === "error") ?? [];
  return (
    <Modal title="Push to Jira" onClose={onClose}>
      <div className="row gap">
        <label className="field"><span>Jira project</span>
          <input className="mono" value={key} maxLength={10} onChange={(e) => setKey(e.target.value.toUpperCase())} placeholder="SHOP" />
        </label>
        <label className="row gap small" title={aiAvailable ? "Let the AI write the stories, criteria and estimates" : "AI is off: rules derive the stories"}>
          <input type="checkbox" checked={useAi} disabled={!aiAvailable} onChange={(e) => setUseAi(e.target.checked)} /> AI-written stories
        </label>
        <button className="small" onClick={preview} disabled={!key || busy !== ""}>{busy === "plan" ? "Preparing…" : "Preview"}</button>
        <button className="small" onClick={refresh} disabled={busy !== ""} title="Read each linked issue's status from Jira">
          {busy === "refresh" ? "Reading…" : "Refresh status"}</button>
      </div>
      {plan && (
        <>
          <p className="small">
            <b>Preview (nothing sent yet):</b> {plan.by_type.epic} epics, {plan.by_type.story} stories, {plan.by_type.subtask} sub-tasks,
            {" "}{plan.by_type.link} links · <b>{plan.counts.create}</b> to create, <b>{plan.counts.update}</b> to update,
            {" "}{plan.counts.skip} unchanged · {plan.engine === "llm" ? "written by AI" : "written by rules"}
          </p>
          <p className="muted tiny">{plan.mapping}</p>
          <ul className="jira-plan">
            {plan.items.map((i) => (
              <li key={i.ref} className={`jira-item ${i.type} ${i.action}`}>
                <button className="ghost jira-row" onClick={() => setOpen(open === i.ref ? null : i.ref)} aria-expanded={open === i.ref}>
                  <span className={`jira-action ${i.action}`}>{i.action === "skip" ? "unchanged" : i.action}</span>
                  <span className="muted small">{TYPE_LABEL[i.type]}</span>
                  <span className="grow">{i.summary}</span>
                  {i.story_points ? <span className="chip small" title={i.points_basis}>{i.story_points} pts</span> : null}
                  {i.external_key && <a className="mono small" href={i.external_url || undefined} target="_blank" rel="noreferrer"
                    onClick={(e) => e.stopPropagation()}>{i.external_key}</a>}
                </button>
                {open === i.ref && i.type !== "link" && (
                  <div className="jira-detail small">
                    {i.acceptance_criteria && i.acceptance_criteria.length > 0 && (
                      <ul>{i.acceptance_criteria.map((c, k) => <li key={k}><b>Given</b> {c.given}, <b>when</b> {c.when}, <b>then</b> {c.then}</li>)}</ul>
                    )}
                    {i.points_basis && <p className="muted">Estimate: {i.points_basis}</p>}
                    {(i.labels?.length ?? 0) > 0 && <p className="muted">Labels: {i.labels!.join(", ")}</p>}
                    {(i.components?.length ?? 0) > 0 && <p className="muted">Components: {i.components!.join(", ")}</p>}
                    <pre className="jira-desc">{i.description}</pre>
                  </div>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
      {result && (
        <p className={`small ${result.counts.failed ? "warn" : "ok"}`}>
          Pushed to {result.project_key}: {result.counts.created} created, {result.counts.updated} updated, {result.counts.unchanged} unchanged
          {result.counts.failed > 0 && <>, {result.counts.failed} failed. Push again to retry only those.</>}
        </p>
      )}
      {failed.length > 0 && <ul className="small error">{failed.map((f) => <li key={f.ref}>{f.summary}: {f.error}</li>)}</ul>}
      {error && <p className="error small">{error}</p>}
      <div className="row end gap">
        <button onClick={onClose}>Close</button>
        <button className="primary" onClick={push} disabled={!plan || busy !== "" || plan.counts.create + plan.counts.update === 0}>
          {busy === "push" ? "Pushing…" : plan ? `Push ${plan.counts.create + plan.counts.update} change${plan.counts.create + plan.counts.update === 1 ? "" : "s"}` : "Push"}
        </button>
      </div>
    </Modal>
  );
}
