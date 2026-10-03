import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { ArchitecturesCard, ArchOption, ChatTurn, ProjectState, QuestionCard, User } from "../types";
import { TYPE_META } from "./ArchNode";

interface Props {
  pid: string;
  branch: string;
  user: User;
  canvasEmpty: boolean;
  aiAvailable: boolean;
  onApplied: (r: { branch: string; version: number; summary: string[]; state: ProjectState }) => void;
}

const storeKey = (pid: string, branch: string) => `archflow.chat.${pid}.${branch}`;

function load(pid: string, branch: string): ChatTurn[] {
  try { return JSON.parse(localStorage.getItem(storeKey(pid, branch)) ?? "[]"); } catch { return []; }
}
function save(pid: string, branch: string, turns: ChatTurn[]) {
  try { localStorage.setItem(storeKey(pid, branch), JSON.stringify(turns)); } catch { /* storage unavailable */ }
}

/** Guided design chat: the assistant asks multiple-choice questions, then offers architectures to pick from. */
export function DesignChat({ pid, branch, user, canvasEmpty, aiAvailable, onApplied }: Props) {
  const [turns, setTurns] = useState<ChatTurn[]>(() => load(pid, branch));
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [engine, setEngine] = useState("");
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => { save(pid, branch, turns); }, [pid, branch, turns]);
  useEffect(() => { listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" }); }, [turns, busy]);

  const step = async (next: ChatTurn[]) => {
    setTurns(next);
    setBusy(true);
    setError("");
    try {
      const r = await api.chat(pid, branch, next, user.name);
      setEngine(r.engine);
      setTurns([...next, { role: "assistant", content: r.message, card: r.card }]);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const sendText = (e?: React.FormEvent) => {
    e?.preventDefault();
    const t = text.trim();
    if (!t || busy) return;
    setText("");
    step([...turns, { role: "user", content: t }]);
  };

  const answer = (option_ids: string[], other: string, labels: string[]) =>
    step([...turns, { role: "user", content: labels.join(", "), answer: { option_ids, other: other || undefined } }]);

  const apply = async (card: ArchitecturesCard, option: ArchOption, mode: "replace" | "branch") => {
    setBusy(true);
    setError("");
    try {
      const r = await api.chatApply(pid, branch, option, card.requirements, mode, user.name);
      const note: ChatTurn = {
        role: "assistant",
        content: mode === "branch"
          ? `Created draft “${r.branch}” with ${option.name} (v${r.version}). Switching to it…`
          : `Applied ${option.name} to ${r.branch} and saved it as v${r.version}.`,
        applied: { option: option.name, branch: r.branch, version: r.version },
      };
      const next = [...turns, { role: "user" as const, content: `Use ${option.name}` }, note];
      setTurns(next);
      if (mode === "branch") save(pid, r.branch, next);  // continue the same conversation on the draft
      onApplied(r);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const reset = () => { setTurns([]); setError(""); };
  const lastIdx = turns.length - 1;

  return (
    <div className="design-chat">
      {!aiAvailable && (
        <div className="dc-banner" role="note">
          <b>AI is off.</b> Questions and architectures come from built-in templates, so they won't adapt to your idea.
          Add <code>GEMINI_API_KEY=…</code> to <code>backend/.env</code> and restart the API to turn it on.
        </div>
      )}
      <div className="dc-list" ref={listRef}>
        {turns.length === 0 && (
          <div className="dc-empty">
            <h3>Design with ArchFlow</h3>
            <p className="muted small">Describe what you want to build. I'll ask a few multiple-choice questions, then show
              you architecture options to pick from.</p>
            {["AI customer-support platform on WhatsApp and web", "Food delivery marketplace with live tracking",
              "Internal knowledge assistant over company documents"].map((s) => (
              <button key={s} className="dc-suggest" onClick={() => step([{ role: "user", content: s }])}>{s}</button>
            ))}
          </div>
        )}
        {turns.map((t, i) => {
          if (t.role === "user") {
            return t.content ? <div key={i} className="dc-msg user">{t.content}</div> : null;
          }
          const active = i === lastIdx && !busy;
          return (
            <div key={i} className="dc-msg bot">
              {t.content && <div>{t.content}</div>}
              {t.card?.type === "question" && (
                <QuestionView card={t.card} active={active} answered={turns[i + 1]?.answer}
                  onAnswer={answer} onSkip={() => step([...turns, { role: "user", content: "Skip — show me the architecture options" }])} />
              )}
              {t.card?.type === "architectures" && (
                <ArchitecturesView card={t.card} disabled={busy} canvasEmpty={canvasEmpty}
                  onApply={(o, mode) => apply(t.card as ArchitecturesCard, o, mode)} />
              )}
            </div>
          );
        })}
        {busy && <div className="dc-msg bot muted pulse">Thinking…</div>}
        {error && <div className="error small pad">{error}</div>}
      </div>
      <form className="dc-input" onSubmit={sendText}>
        <textarea rows={2} value={text} placeholder={turns.length ? "Reply, add detail, or say “show options”…" : "Describe your product or system…"}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendText(); } }} />
        <div className="row between">
          <span className="muted tiny">{engine === "offline" ? "Offline interviewer (no LLM key)" : engine ? "AI interviewer" : ""}</span>
          <span className="row gap">
            {turns.length > 0 && <button type="button" className="small" onClick={reset}>New</button>}
            <button className="primary small" disabled={!text.trim() || busy}>Send</button>
          </span>
        </div>
      </form>
    </div>
  );
}

function QuestionView({ card, active, answered, onAnswer, onSkip }: {
  card: QuestionCard; active: boolean; answered?: { option_ids: string[]; other?: string };
  onAnswer: (ids: string[], other: string, labels: string[]) => void; onSkip: () => void;
}) {
  const [picked, setPicked] = useState<string[]>([]);
  const [other, setOther] = useState("");
  const [showOther, setShowOther] = useState(false);

  if (answered) {
    const labels = card.options.filter((o) => answered.option_ids.includes(o.id)).map((o) => o.label);
    if (answered.other) labels.push(answered.other);
    return (
      <div className="q-card done">
        <div className="q-title">{card.question}</div>
        <div className="row gap">{labels.map((l) => <span key={l} className="chip on">{l}</span>)}</div>
      </div>
    );
  }

  const toggle = (id: string) => {
    if (!active) return;
    if (card.multi) setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));
    else {
      const o = card.options.find((x) => x.id === id)!;
      onAnswer([id], "", [o.label]);  // single choice submits immediately
    }
  };
  const submit = () => {
    const labels = card.options.filter((o) => picked.includes(o.id)).map((o) => o.label);
    onAnswer(picked, other.trim(), other.trim() ? [...labels, other.trim()] : labels);
  };
  const recommended = card.options.find((o) => o.recommended);

  return (
    <div className={`q-card ${active ? "" : "inactive"}`}>
      <div className="row between">
        <div className="q-title">{card.question}</div>
        {card.step && <span className="muted tiny">{card.step}{card.total ? `/${card.total}` : ""}</span>}
      </div>
      {card.why && <div className="muted tiny">{card.why}</div>}
      <div className="q-options" role={card.multi ? "group" : "radiogroup"} aria-label={card.question}>
        {card.options.map((o) => {
          const on = picked.includes(o.id);
          return (
            <button key={o.id} type="button" disabled={!active}
              className={`q-option ${on ? "on" : ""}`} role={card.multi ? "checkbox" : "radio"} aria-checked={on}
              onClick={() => toggle(o.id)}>
              <span className={`q-mark ${card.multi ? "box" : "dot"}`} aria-hidden>{on ? "✓" : ""}</span>
              <span className="q-text">
                <span className="q-label">{o.label}{o.recommended && <span className="badge">Recommended</span>}</span>
                {o.description && <span className="q-desc">{o.description}</span>}
              </span>
            </button>
          );
        })}
      </div>
      {active && (
        <>
          {showOther ? (
            <input autoFocus placeholder="Your own answer…" value={other} onChange={(e) => setOther(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && other.trim()) submit(); }} />
          ) : (
            <button type="button" className="link small" onClick={() => setShowOther(true)}>Other…</button>
          )}
          <div className="row gap q-actions">
            {(card.multi || other.trim()) && (
              <button type="button" className="primary small" disabled={!picked.length && !other.trim()} onClick={submit}>
                Continue{picked.length ? ` (${picked.length})` : ""}
              </button>
            )}
            {recommended && !picked.length && !other && (
              <button type="button" className="small" onClick={() => onAnswer([recommended.id], "", [recommended.label])}>Use recommended</button>
            )}
            <button type="button" className="small ghost" onClick={onSkip}>Skip to options</button>
          </div>
        </>
      )}
    </div>
  );
}

function ArchitecturesView({ card, disabled, canvasEmpty, onApply }: {
  card: ArchitecturesCard; disabled: boolean; canvasEmpty: boolean;
  onApply: (o: ArchOption, mode: "replace" | "branch") => void;
}) {
  const [open, setOpen] = useState<string | null>(card.options[0]?.id ?? null);
  const [confirm, setConfirm] = useState<string | null>(null);
  return (
    <div className="arch-options">
      <div className="q-title">{card.title}</div>
      {card.options.map((o) => {
        const isOpen = open === o.id;
        return (
          <div key={o.id} className={`arch-opt ${isOpen ? "open" : ""}`}>
            <button type="button" className="arch-head" onClick={() => setOpen(isOpen ? null : o.id)} aria-expanded={isOpen}>
              <span className="arch-name">{o.name}{o.recommended && <span className="badge">Recommended</span>}</span>
              <span className="row gap">
                <span className={`lvl l-${o.cost}`}>cost {o.cost}</span>
                <span className={`lvl l-${o.complexity}`}>complexity {o.complexity}</span>
              </span>
            </button>
            {isOpen && (
              <div className="arch-body">
                <p className="small">{o.summary}</p>
                {o.best_for && <p className="tiny muted">Best for: {o.best_for}</p>}
                <div className="pc">
                  <ul className="pros small">{o.pros.map((p) => <li key={p}>{p}</li>)}</ul>
                  <ul className="cons small">{o.cons.map((c) => <li key={c}>{c}</li>)}</ul>
                </div>
                <div className="arch-comps">
                  {o.components.map((c) => (
                    <span key={c.key} className="comp-chip" style={{ ["--accent" as string]: TYPE_META[c.type]?.color ?? "#888" }}
                      title={c.technology}>
                      {TYPE_META[c.type]?.glyph} {c.name}
                    </span>
                  ))}
                </div>
                <div className="row gap q-actions">
                  {confirm === o.id ? (
                    <>
                      <span className="tiny warn">Replaces the components on this canvas (requirements are kept).</span>
                      <button className="small danger" disabled={disabled} onClick={() => { setConfirm(null); onApply(o, "replace"); }}>Replace</button>
                      <button className="small" onClick={() => setConfirm(null)}>Cancel</button>
                    </>
                  ) : (
                    <>
                      <button className="primary small" disabled={disabled}
                        onClick={() => (canvasEmpty ? onApply(o, "replace") : setConfirm(o.id))}>Apply to canvas</button>
                      <button className="small" disabled={disabled} onClick={() => onApply(o, "branch")}
                        title="Create a draft branch with this architecture">Open as draft</button>
                    </>
                  )}
                </div>
              </div>
            )}
          </div>
        );
      })}
      {card.requirements.length > 0 && (
        <p className="tiny muted">{card.requirements.length} requirements from this conversation will be added with the architecture.</p>
      )}
    </div>
  );
}
