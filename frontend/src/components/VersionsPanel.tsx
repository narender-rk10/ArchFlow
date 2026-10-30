import { useState } from "react";
import type { Activity, Branch, Diff, Version } from "../types";

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
  branches?: Branch[];
  /** Open the merge dialog for this source branch (absent when the role cannot edit). */
  onMerge?: (source: string) => void;
}

const BRANCH_RE = /^(?=.{1,60}$)[A-Za-z0-9._-]+(?:\/[A-Za-z0-9._-]+)*$/;

function ago(iso: string) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}

/** Saved versions of the architecture (snapshots you can compare, restore or branch from) and the activity log. */
export function VersionsPanel({ versions, activity, branch, headVersion, diff, onCommit, onDiff, onRestore, onBranchFrom, branches = [], onMerge }: Props) {
  const [msg, setMsg] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const [branchName, setBranchName] = useState("experiment/");
  const [confirmRestore, setConfirmRestore] = useState<number | null>(null);
  const [showActivity, setShowActivity] = useState(false);
  const [showBranches, setShowBranches] = useState(false);

  return (
    <div className="panel">
      <form className="panel-head" onSubmit={(e) => { e.preventDefault(); if (msg.trim()) { onCommit(msg.trim()); setMsg(""); } }}>
        <div className="panel-summary small">Save a version of <b>{branch}</b> to compare or roll back later.</div>
        <div className="row gap">
          <input className="grow" placeholder="What changed? e.g. Added caching" value={msg} onChange={(e) => setMsg(e.target.value)} aria-label="Version description" />
          <button className="primary small" disabled={!msg.trim()}>Save version</button>
        </div>
      </form>

      {diff && (
        <div className="diff-box">
          <div className="row between">
            <b>v{diff.from} → now</b>
            <button className="small" onClick={() => onDiff(null)}>Close</button>
          </div>
          <div className="legend small"><span className="lg added">added</span><span className="lg changed">changed</span><span className="lg removed">removed</span></div>
          <pre className="diff-text">{diff.data.text}</pre>
        </div>
      )}

      {versions.length === 0 && <div className="empty-state"><p className="small">No versions yet. Applying a design option or adopting a decision saves one automatically; you can also save one above.</p></div>}
      <ul className="version-list">
        {versions.map((v) => (
          <li key={v.number} className={`${diff?.from === v.number ? "active" : ""} ${open === v.number ? "open" : ""}`}>
            <button className="version-row" onClick={() => setOpen(open === v.number ? null : v.number)} aria-expanded={open === v.number}>
              <b>v{v.number}</b>
              <span className="grow small version-msg">{v.message}</span>
              {v.number === headVersion && <span className="chip on">latest</span>}
              {v.branch !== branch && <span className="chip mono">{v.branch}</span>}
            </button>
            <div className="muted tiny">{v.author} · {ago(v.created_at)}</div>
            {open === v.number && (
              <div className="version-actions">
                <div className="row gap wrap">
                  <button className="small" onClick={() => onDiff(v.number)}>Compare with now</button>
                  {confirmRestore === v.number ? (
                    <>
                      <button className="small danger" onClick={() => { onRestore(v.number); setConfirmRestore(null); }}>Yes, restore v{v.number}</button>
                      <button className="small" onClick={() => setConfirmRestore(null)}>Cancel</button>
                    </>
                  ) : (
                    <button className="small" onClick={() => setConfirmRestore(v.number)} title="Your current state is saved as a version first">Restore</button>
                  )}
                </div>
                <form className="row gap" onSubmit={(e) => { e.preventDefault(); onBranchFrom(v.number, branchName.trim()); setOpen(null); }}>
                  <input value={branchName} onChange={(e) => setBranchName(e.target.value)} aria-label="New branch name" />
                  <button className="small" disabled={!BRANCH_RE.test(branchName.trim())}>New branch from v{v.number}</button>
                </form>
              </div>
            )}
          </li>
        ))}
      </ul>

      {branches.length > 1 && (
        <>
          <button className="section-toggle" onClick={() => setShowBranches(!showBranches)} aria-expanded={showBranches}>
            {showBranches ? "▾" : "▸"} Branches <span className="muted small">{branches.length}</span>
          </button>
          {showBranches && (
            <ul className="branch-list">
              {branches.map((b) => (
                <li key={b.name}>
                  <span className="grow small mono">⎇ {b.name}</span>
                  {b.name === branch && <span className="chip on">current</span>}
                  {b.base_version !== null && <span className="muted tiny">from v{b.base_version}</span>}
                  {onMerge && <button className="small" onClick={() => onMerge(b.name)} title={`Three-way merge ${b.name} into another branch`}>Merge into…</button>}
                </li>
              ))}
            </ul>
          )}
        </>
      )}

      <button className="section-toggle" onClick={() => setShowActivity(!showActivity)} aria-expanded={showActivity}>
        {showActivity ? "▾" : "▸"} Activity <span className="muted small">{activity.length}</span>
      </button>
      {showActivity && (
        <ul className="activity-list">
          {activity.map((a, i) => (
            <li key={i}>
              <span className="small"><b>{a.actor}</b> {a.summary.join("; ")}</span>
              <span className="muted small"> · {ago(a.created_at)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
