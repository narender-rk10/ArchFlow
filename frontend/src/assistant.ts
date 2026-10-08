import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { Conversation, ConversationMessage } from "./types";

/** The turn being answered right now (shown below the saved messages until the server returns them). */
export interface Draft {
  user: ConversationMessage;
  mode: "ask" | "command";
  answer: string;
  /** Regenerating: the question is already in the list, only the answer is new. */
  regenerate?: boolean;
}

const lastThreadKey = (pid: string) => `archflow.assistant.${pid}`;
const readLast = (pid: string) => { try { return localStorage.getItem(lastThreadKey(pid)); } catch { return null; } };
const writeLast = (pid: string, id: string | null) => {
  try { if (id) localStorage.setItem(lastThreadKey(pid), id); else localStorage.removeItem(lastThreadKey(pid)); }
  catch { /* storage unavailable */ }
};

const tempMessage = (content: string, mode: "ask" | "command", author: string): ConversationMessage => ({
  seq: -1, role: "user", mode, content, data: {}, author, created_at: new Date().toISOString(),
});

/**
 * The project assistant: shared conversation threads stored on the server. Questions stream in; edit commands
 * run against the canvas with the conversation as context. Threads survive reloads and teammates see them.
 */
export function useAssistant(pid: string, branch: string, userName: string,
  /** Called after an edit command was answered (e.g. to tidy the canvas when components were added). */
  onCommandDone?: (message: ConversationMessage) => void) {
  const onDoneRef = useRef(onCommandDone);
  onDoneRef.current = onCommandDone;
  const [threads, setThreads] = useState<Conversation[]>([]);
  const [current, setCurrent] = useState<Conversation | null>(null);
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [error, setError] = useState("");
  const currentRef = useRef<Conversation | null>(null);
  currentRef.current = current;
  const busyRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);

  const refreshThreads = useCallback(() => {
    api.conversations(pid, "assistant").then(setThreads).catch(() => {});
  }, [pid]);

  const open = useCallback(async (id: string | null) => {
    abortRef.current?.abort();
    setError("");
    setDraft(null);
    busyRef.current = false;
    if (!id) { setCurrent(null); setMessages([]); writeLast(pid, null); return; }
    try {
      const { messages: msgs, ...conv } = await api.conversation(pid, id);
      setCurrent(conv);
      setMessages(msgs);
      writeLast(pid, id);
    } catch {
      setCurrent(null);
      setMessages([]);
      writeLast(pid, null);
    }
  }, [pid]);

  // Load the threads and reopen the one you had open in this project.
  useEffect(() => {
    setCurrent(null);
    setMessages([]);
    refreshThreads();
    const last = readLast(pid);
    if (last) open(last);
  }, [pid, refreshThreads, open]);

  const reload = useCallback(async () => {
    const conv = currentRef.current;
    if (!conv || busyRef.current) return;
    try {
      const { messages: msgs, ...fresh } = await api.conversation(pid, conv.id);
      if (currentRef.current?.id === conv.id && !busyRef.current) { setCurrent(fresh); setMessages(msgs); }
    } catch { /* deleted elsewhere: keep what is shown */ }
  }, [pid]);

  const ensureThread = useCallback(async (): Promise<Conversation> => {
    if (currentRef.current) return currentRef.current;
    const conv = await api.createConversation(pid, branch, "assistant");
    setCurrent(conv);
    currentRef.current = conv;
    writeLast(pid, conv.id);
    setThreads((t) => [conv, ...t]);
    return conv;
  }, [pid, branch]);

  const begin = () => {
    if (busyRef.current) return false;
    busyRef.current = true;
    setError("");
    return true;
  };
  const end = () => {
    busyRef.current = false;
    setDraft(null);
    refreshThreads();
  };

  /** Ask a question; the answer streams into ``draft``. Returns false if it could not be sent. */
  const ask = useCallback(async (question: string): Promise<boolean> => {
    if (!begin()) return false;
    const controller = new AbortController();
    abortRef.current = controller;
    let sent: ConversationMessage | null = null;
    try {
      const conv = await ensureThread();
      setDraft({ user: tempMessage(question, "ask", userName), mode: "ask", answer: "" });
      const answer = await api.askStream(pid, branch, conv.id, question, {
        onStart: (u) => { sent = u; setDraft((d) => d && { ...d, user: u }); },
        onDelta: (t) => setDraft((d) => d && { ...d, answer: d.answer + t }),
      }, controller.signal);
      if (currentRef.current?.id === conv.id) {
        setMessages((m) => [...m, ...(sent ? [sent] : []), answer]);
        setCurrent((c) => c && { ...c, title: c.title || question.slice(0, 60) });
      }
      return true;
    } catch (e) {
      if (controller.signal.aborted) return true;
      setError((e as Error).message);
      if (sent) setMessages((m) => [...m, sent!]);
      return sent !== null;
    } finally {
      end();
    }
  }, [pid, branch, userName, ensureThread]);

  /** Apply a canvas edit, with the conversation as context ("now add a cache in front of it"). */
  const command = useCallback(async (text: string, selection: string[]): Promise<boolean> => {
    if (!begin()) return false;
    try {
      const conv = await ensureThread();
      setDraft({ user: tempMessage(text, "command", userName), mode: "command", answer: "" });
      const r = await api.conversationCommand(pid, branch, conv.id, text, selection);
      if (currentRef.current?.id === conv.id) {
        setMessages((m) => [...m, r.user_message, r.message]);
        setCurrent((c) => c && { ...c, title: c.title || text.slice(0, 60) });
      }
      onDoneRef.current?.(r.message);
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    } finally {
      end();
    }
  }, [pid, branch, userName, ensureThread]);

  /** Answer the last question again. */
  const regenerate = useCallback(async () => {
    const conv = currentRef.current;
    const lastQ = [...messages].reverse().find((m) => m.role === "user");
    if (!conv || !lastQ || lastQ.mode !== "ask" || !begin()) return;
    const controller = new AbortController();
    abortRef.current = controller;
    const kept = messages.filter((m) => m.seq <= lastQ.seq);
    setMessages(kept);
    setDraft({ user: lastQ, mode: "ask", answer: "", regenerate: true });
    try {
      const answer = await api.regenerateStream(pid, branch, conv.id, {
        onDelta: (t) => setDraft((d) => d && { ...d, answer: d.answer + t }),
      }, controller.signal);
      if (currentRef.current?.id === conv.id) setMessages([...kept, answer]);
    } catch (e) {
      if (!controller.signal.aborted) { setError((e as Error).message); reload(); }
    } finally {
      end();
    }
  }, [pid, branch, messages, reload]);

  const rename = useCallback(async (title: string) => {
    const conv = currentRef.current;
    if (!conv || !title.trim()) return;
    try {
      const fresh = await api.renameConversation(pid, conv.id, title.trim());
      setCurrent((c) => c && { ...c, title: fresh.title });
      refreshThreads();
    } catch (e) { setError((e as Error).message); }
  }, [pid, refreshThreads]);

  const remove = useCallback(async (id: string) => {
    try {
      await api.deleteConversation(pid, id);
      if (currentRef.current?.id === id) open(null);
      setThreads((t) => t.filter((c) => c.id !== id));
    } catch (e) { setError((e as Error).message); }
  }, [pid, open]);

  const clear = useCallback(async () => {
    const conv = currentRef.current;
    if (!conv) return;
    try {
      await api.clearConversation(pid, conv.id);
      setMessages([]);
      setCurrent((c) => c && { ...c, summary: "", summarized_seq: 0, message_count: 0 });
      refreshThreads();
    } catch (e) { setError((e as Error).message); }
  }, [pid, refreshThreads]);

  /** A conversation changed on the server (a teammate's turn, or the summary of yours). */
  const onRemote = useCallback((id: string) => {
    refreshThreads();
    if (id === currentRef.current?.id) window.setTimeout(reload, 300);
  }, [refreshThreads, reload]);

  return {
    threads, current, messages, draft, error, busy: draft !== null,
    open, ask, command, regenerate, rename, remove, clear, onRemote, refreshThreads,
    dismissError: () => setError(""),
  };
}

export type Assistant = ReturnType<typeof useAssistant>;
