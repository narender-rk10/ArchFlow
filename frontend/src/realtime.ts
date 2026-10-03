import { useEffect, useRef, useState } from "react";
import type { User } from "./types";

interface Handlers {
  onVersions: () => void;
  onActivity: () => void;
}

/**
 * Server events per (project, branch): AI status, new versions and activity.
 * Canvas content, presence, cursors and selections sync through Yjs (see collab.ts).
 */
export function useRealtime(pid: string, branch: string, user: User, handlers: Handlers) {
  const [aiStatus, setAiStatus] = useState<{ status: string; actor?: string; text?: string }>({ status: "idle" });
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;

  useEffect(() => {
    let closed = false;
    let retry: number | undefined;
    let ws: WebSocket | null = null;

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const qs = new URLSearchParams({ name: user.name, color: user.color });
      ws = new WebSocket(`${proto}://${location.host}/ws/${pid}/${encodeURIComponent(branch)}?${qs}`);
      ws.onclose = () => { if (!closed) retry = window.setTimeout(connect, 1500); };
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.type === "state" || msg.type === "activity") handlersRef.current.onActivity();
        else if (msg.type === "versions") handlersRef.current.onVersions();
        else if (msg.type === "ai_status") setAiStatus(msg);
      };
    };
    connect();
    return () => {
      closed = true;
      window.clearTimeout(retry);
      ws?.close();
    };
  }, [pid, branch, user.name, user.color]);

  return { aiStatus };
}
