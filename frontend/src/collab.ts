import { HocuspocusProvider } from "@hocuspocus/provider";
import { useCallback, useEffect, useRef, useState } from "react";
import * as Y from "yjs";
import { applyOps, coll, docToState, ORIGIN_LOCAL, ORIGIN_SERVER } from "@collab/ydoc";
import type { Op, ProjectState, User } from "./types";

export interface Peer { clientId: number; name: string; color: string; cursor?: { x: number; y: number }; selection?: string[] }

interface Session { doc: Y.Doc; provider: HocuspocusProvider; undo: Y.UndoManager }

/**
 * Live collaborative document for one project branch.
 * The canvas reads `state` (derived from the Yjs doc) and writes through `apply(ops)`; Yjs merges
 * everyone's edits, and awareness carries presence, cursors and selections.
 */
export function useCollab(pid: string, branch: string, user: User) {
  const [state, setState] = useState<ProjectState | null>(null);
  const [synced, setSynced] = useState(false);
  const [status, setStatus] = useState<"connecting" | "connected" | "disconnected">("connecting");
  const [peers, setPeers] = useState<Peer[]>([]);
  const [selfId, setSelfId] = useState(0);
  const [canUndo, setCanUndo] = useState(false);
  const [canRedo, setCanRedo] = useState(false);
  const [lastRemote, setLastRemote] = useState<{ by: "server" | "peer"; at: number } | null>(null);
  const session = useRef<Session | null>(null);

  // The effect owns the connection's whole lifecycle (StrictMode-safe: it may run, clean up and run again).
  useEffect(() => {
    const doc = new Y.Doc();
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const provider = new HocuspocusProvider({
      url: `${proto}://${location.host}/collab`,
      name: `${pid}:${branch}`,
      document: doc,
      token: JSON.stringify({ name: user.name, color: user.color }),
    });
    const undo = new Y.UndoManager([coll(doc, "nodes"), coll(doc, "edges"), coll(doc, "requirements")], {
      trackedOrigins: new Set([ORIGIN_LOCAL]),
      captureTimeout: 400,
    });
    session.current = { doc, provider, undo };
    setSelfId(doc.clientID);
    setSynced(false);
    setState(null);

    let rev = 0;
    let frame = 0;
    const publish = () => {
      frame = 0;
      rev += 1;
      setState({ ...(docToState(doc) as unknown as ProjectState), rev });
    };
    const onUpdate = (_u: Uint8Array, origin: unknown) => {
      if (origin !== ORIGIN_LOCAL) setLastRemote({ by: origin === ORIGIN_SERVER ? "server" : "peer", at: Date.now() });
      if (!frame) frame = requestAnimationFrame(publish);
    };
    const onSynced = () => { setSynced(true); publish(); };
    const onStatus = (e: { status: "connecting" | "connected" | "disconnected" }) => setStatus(e.status);
    doc.on("update", onUpdate);
    provider.on("synced", onSynced);
    provider.on("status", onStatus);

    const awareness = provider.awareness!;
    awareness.setLocalStateField("user", { name: user.name, color: user.color });
    const onAwareness = () => {
      const list: Peer[] = [];
      awareness.getStates().forEach((s, clientId) => {
        if (s.user) list.push({ clientId, name: s.user.name, color: s.user.color, cursor: s.cursor, selection: s.selection });
      });
      setPeers(list);
    };
    awareness.on("change", onAwareness);
    onAwareness();

    const onStack = () => { setCanUndo(undo.undoStack.length > 0); setCanRedo(undo.redoStack.length > 0); };
    undo.on("stack-item-added", onStack);
    undo.on("stack-item-popped", onStack);

    return () => {
      cancelAnimationFrame(frame);
      doc.off("update", onUpdate);
      provider.off("synced", onSynced);
      provider.off("status", onStatus);
      awareness.off("change", onAwareness);
      undo.destroy();
      provider.destroy();
      doc.destroy();
      if (session.current?.doc === doc) session.current = null;
    };
  }, [pid, branch, user.name, user.color]);

  /** Apply UI operations to the shared document. Throws OpError on invalid batches (nothing is changed). */
  const apply = useCallback((ops: Op[]) => {
    if (!session.current) throw new Error("Not connected to the live document yet");
    return applyOps(session.current.doc, ops, ORIGIN_LOCAL);
  }, []);
  const setCursor = useCallback((cursor: { x: number; y: number } | null) => {
    session.current?.provider.awareness?.setLocalStateField("cursor", cursor ?? undefined);
  }, []);
  const setSelection = useCallback((ids: string[]) => {
    session.current?.provider.awareness?.setLocalStateField("selection", ids);
  }, []);
  const undo = useCallback(() => { session.current?.undo.undo(); }, []);
  const redo = useCallback(() => { session.current?.undo.redo(); }, []);

  return { state, synced, status, peers, selfId, lastRemote, apply, setCursor, setSelection, undo, redo, canUndo, canRedo };
}
