import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { AiSuggestion, SuggestionMode } from "./types";

export interface SuggestionRequest {
  mode: SuggestionMode;
  selection?: string[];
  conversationId?: string | null;
  lastQuestion?: string;
  lastAnswer?: string;
}

/**
 * Suggestions written by the LLM for this project (server: suggestions.py, prompt `assistant.suggestions`): starting
 * prompts, or follow-ups when a last exchange is given. `items` is null while loading or when no AI is configured, so
 * callers show their offline suggestions instead. `version` (the project state's revision) refreshes them after edits;
 * the server caches per state, so repeated renders cost nothing.
 */
export function useAiSuggestions(pid: string, branch: string, req: SuggestionRequest, enabled: boolean, version: number | undefined) {
  const [items, setItems] = useState<AiSuggestion[] | null>(null);
  const [loading, setLoading] = useState(false);
  const seq = useRef(0);
  const key = JSON.stringify([pid, branch, req.mode, [...(req.selection ?? [])].sort(), req.conversationId ?? null,
    req.lastQuestion ?? "", (req.lastAnswer ?? "").slice(0, 4000), version]);

  useEffect(() => {
    if (!enabled) { setItems(null); setLoading(false); return; }
    const my = ++seq.current;
    setLoading(true);
    // Debounced: selection and canvas edits arrive in bursts.
    const t = window.setTimeout(() => {
      api.suggestions(pid, branch, {
        mode: req.mode, selection: req.selection ?? [], conversation_id: req.conversationId ?? null,
        last_question: req.lastQuestion ?? "", last_answer: (req.lastAnswer ?? "").slice(0, 4000),
      })
        .then((r) => { if (my === seq.current) setItems(r.engine === "llm" && r.suggestions.length ? r.suggestions : null); })
        .catch(() => { if (my === seq.current) setItems(null); })
        .finally(() => { if (my === seq.current) setLoading(false); });
    }, 700);
    return () => window.clearTimeout(t);
  }, [key, enabled]); // eslint-disable-line react-hooks/exhaustive-deps

  return { items, loading };
}

/** Offline (rule-based) prompts in the same shape as AI suggestions. */
export const asSuggestions = (texts: string[]): AiSuggestion[] => texts.map((t) => ({ label: t, prompt: t }));
