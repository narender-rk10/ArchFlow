import type { Analysis, Issue } from "../types";

interface Props {
  analysis: Analysis | null;
  onFocus: (issue: Issue) => void;
  onAsk: (question: string) => void;
}

function Meter({ label, value }: { label: string; value: number }) {
  const tone = value >= 80 ? "good" : value >= 50 ? "mid" : "bad";
  return (
    <div className="meter">
      <div className="row between small"><span>{label}</span><b>{value}%</b></div>
      <div className="meter-bar"><div className={`meter-fill ${tone}`} style={{ width: `${value}%` }} /></div>
    </div>
  );
}

export function IssuesPanel({ analysis, onFocus, onAsk }: Props) {
  if (!analysis) return <div className="panel"><p className="muted small pad">Analysing…</p></div>;
  const h = analysis.health;
  const all = [...analysis.architecture_issues, ...analysis.requirement_issues];
  const order = { error: 0, warning: 1, info: 2 } as const;
  all.sort((a, b) => order[a.severity] - order[b.severity]);

  return (
    <div className="panel">
      <div className="health pad">
        <Meter label="Requirements complete" value={h.requirements_complete} />
        <Meter label="Requirements approved" value={h.requirements_approved} />
        <Meter label="Traceability" value={h.traceability} />
        <Meter label="Architecture checks" value={h.architecture_score} />
        <div className="small muted">
          {h.counts.requirements} requirements · {h.counts.components} components · {h.counts.connections} connections
        </div>
      </div>
      <div className="row gap pad">
        <button className="small" onClick={() => onAsk("Review this architecture against the requirements. What are the biggest risks, failure modes, bottlenecks and missing pieces? Suggest concrete changes.")}>AI architecture review</button>
        <button className="small" onClick={() => onAsk("What happens if the database fails? And if the LLM is unavailable?")}>Failure modes</button>
      </div>
      <h4 className="pad">Issues ({all.length})</h4>
      {all.length === 0 && <p className="muted small pad">No issues found.</p>}
      <ul className="issue-list">
        {all.map((i, k) => (
          <li key={k} className={`issue ${i.severity}`} onClick={() => onFocus(i)}>
            <div><span className="sev">{i.severity}</span> {i.message}</div>
            {i.suggestion && <div className="small muted">{i.suggestion}</div>}
          </li>
        ))}
      </ul>
    </div>
  );
}
