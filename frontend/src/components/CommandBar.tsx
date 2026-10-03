import { useEffect, useRef, useState } from "react";

export interface LogEntry {
  id: number;
  kind: "command" | "ask";
  text: string;
  pending?: boolean;
  explanation?: string;
  summary?: string[];
  questions?: string[];
  answer?: string;
  error?: string;
  engine?: string;
}

interface Props {
  log: LogEntry[];
  selectionCount: number;
  aiBusy: string | null;
  onCommand: (text: string) => void;
  onAsk: (text: string) => void;
}

const SUGGESTIONS = [
  "Add Redis between Order Service and Orders DB",
  "Add a payment service connected to API Gateway",
  "Convert this to an event-driven architecture with Kafka",
  "Add authentication and PII protection requirements",
];

export function CommandBar({ log, selectionCount, aiBusy, onCommand, onAsk }: Props) {
  const [text, setText] = useState("");
  const [mode, setMode] = useState<"command" | "ask">("command");
  const [expanded, setExpanded] = useState(true);
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: "smooth" });
  }, [log]);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const t = text.trim();
    if (!t) return;
    (mode === "command" ? onCommand : onAsk)(t);
    setText("");
    setExpanded(true);
  };

  return (
    <div className="command-bar">
      {expanded && log.length > 0 && (
        <div className="cmd-log" ref={logRef}>
          {log.map((e) => (
            <div key={e.id} className="cmd-entry">
              <div className="cmd-you">{e.kind === "ask" ? "?" : "›"} {e.text}</div>
              {e.pending && <div className="muted small pulse">AI is working…</div>}
              {e.error && <div className="error small">{e.error}</div>}
              {e.summary && e.summary.length > 0 && (
                <ul className="small cmd-changes">{e.summary.map((s, i) => <li key={i}>{s}</li>)}</ul>
              )}
              {e.explanation && <div className="small">{e.explanation}</div>}
              {e.questions && e.questions.length > 0 && (
                <div className="cmd-questions small">
                  <b>Questions from ArchFlow:</b>
                  <ul>{e.questions.map((q, i) => <li key={i}><button className="link" onClick={() => { setText(`${q} → `); setMode("command"); }}>{q}</button></li>)}</ul>
                </div>
              )}
              {e.answer && <div className="small answer">{e.answer}</div>}
              {e.engine && <div className="muted tiny">{e.engine === "offline" ? "offline parser" : "AI"}</div>}
            </div>
          ))}
        </div>
      )}
      <form className="cmd-form" onSubmit={submit}>
        <div className="seg">
          <button type="button" className={mode === "command" ? "on" : ""} onClick={() => setMode("command")}>Edit</button>
          <button type="button" className={mode === "ask" ? "on" : ""} onClick={() => setMode("ask")}>Ask</button>
        </div>
        <input
          list="cmd-suggestions"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={mode === "command"
            ? (selectionCount ? `Tell ArchFlow what to change (${selectionCount} selected)…` : "Tell ArchFlow what to change, or describe your product idea…")
            : "Ask about the project: “Why was Redis added?”, “Which components handle PII?”"}
        />
        <datalist id="cmd-suggestions">{SUGGESTIONS.map((s) => <option key={s} value={s} />)}</datalist>
        <button className="primary" disabled={!text.trim()}>{mode === "command" ? "Apply" : "Ask"}</button>
        {log.length > 0 && (
          <button type="button" className="ghost" onClick={() => setExpanded(!expanded)} aria-label="toggle log">{expanded ? "▾" : "▴"}</button>
        )}
      </form>
      {aiBusy && <div className="ai-busy small">✦ {aiBusy}</div>}
    </div>
  );
}
