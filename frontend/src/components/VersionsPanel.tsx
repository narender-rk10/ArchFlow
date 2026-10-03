import { useState } from "react";
import type { Activity, Diff, Version } from "../types";

interface Props {
  versions: Version[];
  activity: Activity[];
  branch: string;
  headVersion: number | null;
  diff: { from: number; data: Diff } | null;
  onCommit: (message: string) => void;
  onDiff: (v: number | null) => void;
  onRestore: (v: number) => void;
  onBranchFrom: (v: number, name: string) => void;
}

function ago(iso: string) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(iso).toLocaleDateString();
}

export function VersionsPanel({ versions, activity, branch, headVersion, diff, onCommit, onDiff, onRestore, onBranchFrom }: Props) {
  const [msg, setMsg] = useState("");
  const [branchFor, setBranchFor] = useState<number | null>(null);
  const [branchName, setBranchName] = useState("experiment/");
  const [confirmRestore, setConfirmRestore] = useState<number | null>(null);

  return (
    <div className="panel">
      <form className="panel-tools" onSubmit={(e) => { e.preventDefault(); if (msg.trim()) { onCommit(msg.trim()); setMsg(""); } }}>
        <input placeholder={`Describe this version (on ${branch})…`} value={msg} onChange={(e) => setMsg(e.target.value)} />
        <button className="primary" disabled={!msg.trim()}>Commit</button>
      </form>

      {diff && (
        <div className="diff-box">
          <div className="row between">
            <b>v{diff.from} → working</b>
            <button className="small" onClick={() => onDiff(null)}>Exit diff</button>
          </div>
          <div className="legend small"><span className="lg added">added</span><span className="lg changed">changed</span><span className="lg removed">removed</span></div>
          <pre className="diff-text">{diff.data.text}</pre>
        </div>
      )}

      <h4 className="pad">Versions</h4>
      {versions.length === 0 && <p className="muted small pad">No versions yet. Commit to snapshot the architecture.</p>}
      <ul className="version-list">
        {versions.map((v) => (
          <li key={v.number} className={diff?.from === v.number ? "active" : ""}>
            <div className="row between">
              <span><b>v{v.number}</b> <span className="chip">{v.branch}</span>{v.number === headVersion && <span className="chip on">head</span>}</span>
              <span className="muted small">{ago(v.created_at)}</span>
            </div>
            <div className="small">{v.message}</div>
            <div className="muted small">by {v.author}</div>
            <div className="row gap">
              <button className="small" onClick={() => onDiff(v.number)}>Diff vs working</button>
              {confirmRestore === v.number ? (
                <>
                  <button className="small danger" onClick={() => { onRestore(v.number); setConfirmRestore(null); }}>Confirm restore</button>
                  <button className="small" onClick={() => setConfirmRestore(null)}>Cancel</button>
                </>
              ) : (
                <button className="small" onClick={() => setConfirmRestore(v.number)}>Restore</button>
              )}
              <button className="small" onClick={() => setBranchFor(branchFor === v.number ? null : v.number)}>Branch…</button>
            </div>
            {branchFor === v.number && (
              <form className="row gap" onSubmit={(e) => { e.preventDefault(); onBranchFrom(v.number, branchName.trim()); setBranchFor(null); }}>
                <input value={branchName} onChange={(e) => setBranchName(e.target.value)} />
                <button className="small primary" disabled={!/^(?=.{1,60}$)[A-Za-z0-9._-]+(?:\/[A-Za-z0-9._-]+)*$/.test(branchName.trim())}>Create</button>
              </form>
            )}
          </li>
        ))}
      </ul>

      <h4 className="pad">Activity</h4>
      <ul className="activity-list">
        {activity.map((a, i) => (
          <li key={i}>
            <span className="small"><b>{a.actor}</b> {a.summary.join("; ")}</span>
            <span className="muted small"> · {ago(a.created_at)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
