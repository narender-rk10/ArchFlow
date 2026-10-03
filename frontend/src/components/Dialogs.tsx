import { useEffect, useState } from "react";
import { api } from "../api";

export function PromptDialog({ pid, branch, nodeIds, requirementIds, onClose }: {
  pid: string; branch: string; nodeIds: string[]; requirementIds: string[]; onClose: () => void;
}) {
  const [targets, setTargets] = useState<{ key: string; name: string }[]>([]);
  const [target, setTarget] = useState("claude-code");
  const [extra, setExtra] = useState("");
  const [prompt, setPrompt] = useState("");
  const [copied, setCopied] = useState(false);

  useEffect(() => { api.promptTargets().then(setTargets).catch(() => {}); }, []);
  useEffect(() => {
    const t = window.setTimeout(() => {
      api.prompt(pid, branch, nodeIds, requirementIds, target, extra).then((r) => setPrompt(r.prompt)).catch((e) => setPrompt(String(e.message)));
    }, 250);
    return () => window.clearTimeout(t);
  }, [pid, branch, nodeIds, requirementIds, target, extra]);

  const copy = async () => {
    try { await navigator.clipboard.writeText(prompt); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch { /* ignore */ }
  };
  const download = () => {
    const url = URL.createObjectURL(new Blob([prompt], { type: "text/markdown" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: "archflow-implementation-brief.md" });
    a.click();
    URL.revokeObjectURL(url);
  };

  const scope = nodeIds.length || requirementIds.length
    ? `${nodeIds.length} component(s), ${requirementIds.length} requirement(s) + traced context`
    : "entire architecture";

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal wide" role="dialog" aria-label="Vibe coding prompt" onClick={(e) => e.stopPropagation()}>
        <div className="row between">
          <h3>Vibe coding — implementation brief</h3>
          <button className="ghost" onClick={onClose} aria-label="close">✕</button>
        </div>
        <p className="muted small">Scope: {scope}. Paste into your coding agent, or save as a task file.</p>
        <div className="row gap">
          <select value={target} onChange={(e) => setTarget(e.target.value)}>
            {targets.map((t) => <option key={t.key} value={t.key}>{t.name}</option>)}
          </select>
          <input className="grow" placeholder="Extra instructions (optional)" value={extra} onChange={(e) => setExtra(e.target.value)} />
        </div>
        <pre className="prompt-out">{prompt}</pre>
        <div className="row gap end">
          <button onClick={download}>Download .md</button>
          <button className="primary" onClick={copy}>{copied ? "Copied ✓" : "Copy prompt"}</button>
        </div>
      </div>
    </div>
  );
}
