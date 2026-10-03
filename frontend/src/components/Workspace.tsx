import {
  Background, BackgroundVariant, Controls, MarkerType, MiniMap, ReactFlow, ReactFlowProvider, ViewportPortal,
  applyEdgeChanges, applyNodeChanges, useReactFlow,
  type Connection, type Edge, type EdgeChange, type NodeChange, type OnDelete,
} from "@xyflow/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { useCollab } from "../collab";
import { useRealtime } from "../realtime";
import type {
  Activity, Analysis, ArchNode, Branch, Catalog, CatalogItem, Diff, Issue, Op, Project, User, Version,
} from "../types";
import { ArchNodeComponent, TYPE_META, type ArchFlowNode, type DiffMark } from "./ArchNode";
import { CommandBar, type LogEntry } from "./CommandBar";
import { ConnectDialog } from "./ConnectDialog";
import { DesignChat } from "./DesignChat";
import { PromptDialog } from "./Dialogs";
import { Inspector } from "./Inspector";
import { IssuesPanel } from "./IssuesPanel";
import { DRAG_MIME, Palette } from "./Palette";
import { RequirementsPanel } from "./RequirementsPanel";
import { VersionsPanel } from "./VersionsPanel";

interface Props {
  pid: string;
  branch: string;
  user: User;
  colorMode: "light" | "dark";
  themeToggle: React.ReactNode;
  onBranch: (branch: string) => void;
  onExit: () => void;
}

const nodeTypes = { arch: ArchNodeComponent };
type Tab = "design" | "requirements" | "issues" | "versions";
const rand = () => Math.random().toString(36).slice(2, 6);

export function Workspace(props: Props) {
  return (
    <ReactFlowProvider>
      <WorkspaceInner {...props} />
    </ReactFlowProvider>
  );
}

function WorkspaceInner({ pid, branch, user, colorMode, themeToggle, onBranch, onExit }: Props) {
  const flow = useReactFlow<ArchFlowNode, Edge>();
  const [project, setProject] = useState<Project | null>(null);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [headVersion, setHeadVersion] = useState<number | null>(null);
  const [versions, setVersions] = useState<Version[]>([]);
  const [activity, setActivity] = useState<Activity[]>([]);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [tab, setTab] = useState<Tab>("design");
  const [traced, setTraced] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [diff, setDiff] = useState<{ from: number; data: Diff } | null>(null);
  const [promptFor, setPromptFor] = useState<{ nodeIds: string[]; requirementIds: string[] } | null>(null);
  const [log, setLog] = useState<LogEntry[]>([]);
  const [toasts, setToasts] = useState<{ id: number; text: string }[]>([]);
  const [loadError, setLoadError] = useState("");
  const [connectOpen, setConnectOpen] = useState(false);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [converting, setConverting] = useState(false);
  const [ai, setAi] = useState<{ available: boolean; model: string } | null>(null);
  const [newBranch, setNewBranch] = useState<string | null>(null);
  const [nodes, setNodes] = useState<ArchFlowNode[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const clipboard = useRef<{ nodes: ArchNode[]; edges: { source: string; target: string; protocol: string; label: string }[] } | null>(null);
  const wrapper = useRef<HTMLDivElement>(null);

  const toast = useCallback((text: string) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t.slice(-3), { id, text }]);
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 3500);
  }, []);

  // The canvas model is a live Yjs document shared with everyone on this branch (and with AI/MCP edits).
  const live = useCollab(pid, branch, user);
  const state = live.state;
  const stateRef = useRef(state);
  stateRef.current = state;

  const refreshMeta = useCallback(() => {
    api.versions(pid).then(setVersions).catch(() => {});
    api.activity(pid, branch).then(setActivity).catch(() => {});
    api.project(pid, branch).then((p) => { setBranches(p.branches); setHeadVersion(p.head_version); }).catch(() => {});
  }, [pid, branch]);

  // ---- initial load ----
  useEffect(() => {
    api.project(pid, branch).then((p) => {
      setProject(p.project);
      setBranches(p.branches);
      setHeadVersion(p.head_version);
    }).catch((e) => setLoadError(e.message));
    refreshMeta();
    api.catalog().then(setCatalog).catch(() => {});
    api.health().then((h) => setAi({ available: !!h.llm?.available, model: h.llm?.groups?.reasoning?.[0] ?? h.model })).catch(() => {});
  }, [pid, branch, refreshMeta]);

  // ---- realtime ----
  const activityTimer = useRef<number | undefined>(undefined);
  const rt = useRealtime(pid, branch, user, {
    onActivity: () => {
      window.clearTimeout(activityTimer.current);
      activityTimer.current = window.setTimeout(() => api.activity(pid, branch).then(setActivity).catch(() => {}), 400);
    },
    onVersions: refreshMeta,
  });

  // ---- analysis (debounced on every revision) ----
  useEffect(() => {
    if (!state) return;
    // The API's copy of the document trails the live one by the collab store debounce (a second or two).
    const t = window.setTimeout(() => api.analysis(pid, branch).then(setAnalysis).catch(() => {}), 2200);
    return () => window.clearTimeout(t);
  }, [state?.rev, pid, branch]);

  // ---- keep an open diff live ----
  useEffect(() => {
    if (!diff) return;
    api.diff(pid, branch, String(diff.from)).then((data) => setDiff((d) => (d ? { ...d, data } : d))).catch(() => {});
  }, [state?.rev]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- sending operations ----
  const { apply, setCursor, setSelection, undo, redo } = live;
  const send = useCallback((ops: Op[]) => {
    if (!ops.length) return;
    try {
      apply(ops);
    } catch (e) {
      toast(`⚠ ${(e as Error).message}`);
    }
  }, [apply, toast]);

  // ---- derived view state ----
  const issuesByNode = useMemo(() => {
    const m: Record<string, number> = {};
    for (const i of analysis?.architecture_issues ?? []) for (const id of i.node_ids) m[id] = (m[id] ?? 0) + 1;
    return m;
  }, [analysis]);

  const highlight = useMemo<Set<string> | null>(() => {
    if (!state) return null;
    if (traced) return new Set(Object.values(state.nodes).filter((n) => n.requirement_ids.includes(traced)).map((n) => n.id));
    const q = search.trim().toLowerCase();
    if (q) {
      return new Set(Object.values(state.nodes).filter((n) =>
        `${n.name} ${n.technology} ${n.type} ${n.description} ${n.requirement_ids.join(" ")}`.toLowerCase().includes(q)).map((n) => n.id));
    }
    return null;
  }, [state, traced, search]);

  const remoteByNode = useMemo(() => {
    const m: Record<string, { name: string; color: string }[]> = {};
    for (const p of live.peers) {
      if (p.clientId === live.selfId) continue;
      for (const id of p.selection ?? []) (m[id] ??= []).push({ name: p.name, color: p.color });
    }
    return m;
  }, [live.peers, live.selfId]);

  // ---- server model → React Flow nodes/edges ----
  useEffect(() => {
    if (!state) return;
    const d = diff?.data;
    const mark = (id: string, sec?: Diff["nodes"]): DiffMark =>
      !sec ? null : sec.added.some((x) => x.id === id) ? "added" : sec.changed.some((x) => x.id === id) ? "changed" : null;

    setNodes((prev) => {
      const prevMap = new Map(prev.map((n) => [n.id, n]));
      const out: ArchFlowNode[] = Object.values(state.nodes).map((m) => {
        const p = prevMap.get(m.id);
        return {
          ...p, // keep React Flow internals (measured size, selection, dragging)
          id: m.id, type: "arch",
          position: p?.dragging ? p.position : m.position,
          selected: p?.selected ?? false,
          data: {
            model: m, dimmed: !!highlight && !highlight.has(m.id), highlighted: !!highlight?.has(m.id),
            diff: mark(m.id, d?.nodes), remote: remoteByNode[m.id], issues: issuesByNode[m.id],
          },
        };
      });
      if (d) {
        for (const r of d.nodes.removed) {
          const m = d.from_state.nodes[r.id];
          if (m) out.push({ id: `ghost:${m.id}`, type: "arch", position: m.position, draggable: false, selectable: false, connectable: false,
            data: { model: m, diff: "removed" } });
        }
      }
      return out;
    });

    setEdges((prev) => {
      const prevSel = new Map(prev.map((e) => [e.id, e.selected]));
      const out: Edge[] = Object.values(state.edges).map((e) => {
        const dm = mark(e.id, d?.edges);
        const dim = !!highlight && !(highlight.has(e.source) && highlight.has(e.target));
        return {
          id: e.id, source: e.source, target: e.target, label: e.protocol || e.label || undefined,
          selected: prevSel.get(e.id) ?? false, markerEnd: { type: MarkerType.ArrowClosed },
          className: [dm && `diff-${dm}`, dim && "dimmed"].filter(Boolean).join(" "),
        };
      });
      if (d) {
        for (const r of d.edges.removed) {
          const e = d.from_state.edges[r.id];
          if (!e) continue;
          const src = state.nodes[e.source] ? e.source : `ghost:${e.source}`;
          const tgt = state.nodes[e.target] ? e.target : `ghost:${e.target}`;
          out.push({ id: `ghost:${e.id}`, source: src, target: tgt, label: e.protocol || undefined, selectable: false,
            className: "diff-removed", markerEnd: { type: MarkerType.ArrowClosed } });
        }
      }
      return out;
    });
  }, [state, diff, highlight, remoteByNode, issuesByNode]);


  const selectedNodeIds = useMemo(() => nodes.filter((n) => n.selected && !n.id.startsWith("ghost:")).map((n) => n.id), [nodes]);
  const selectedEdgeIds = useMemo(() => edges.filter((e) => e.selected && !e.id.startsWith("ghost:")).map((e) => e.id), [edges]);
  const selKey = selectedNodeIds.join(",");
  useEffect(() => { setSelection(selectedNodeIds); }, [selKey, setSelection]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- canvas handlers ----
  const onNodesChange = useCallback((changes: NodeChange<ArchFlowNode>[]) =>
    setNodes((ns) => applyNodeChanges(changes.filter((c) => c.type !== "remove"), ns)), []);
  const onEdgesChange = useCallback((changes: EdgeChange<Edge>[]) =>
    setEdges((es) => applyEdgeChanges(changes.filter((c) => c.type !== "remove"), es)), []);

  const onDelete: OnDelete<ArchFlowNode, Edge> = useCallback(({ nodes: dn, edges: de }) => {
    const ids = new Set(dn.map((n) => n.id));
    send([
      ...de.filter((e) => !ids.has(e.source) && !ids.has(e.target) && !e.id.startsWith("ghost:")).map((e) => ({ op: "remove_edge", id: e.id })),
      ...dn.filter((n) => !n.id.startsWith("ghost:")).map((n) => ({ op: "remove_node", id: n.id })),
    ]);
  }, [send]);

  const onNodeDragStop = useCallback((_: unknown, __: unknown, dragged: ArchFlowNode[]) => {
    send(dragged.map((n) => ({ op: "update_node", id: n.id, changes: { position: { x: Math.round(n.position.x), y: Math.round(n.position.y) } } })));
  }, [send]);

  const onConnect = useCallback((c: Connection) => {
    if (c.source && c.target && c.source !== c.target) send([{ op: "add_edge", edge: { source: c.source, target: c.target } }]);
  }, [send]);

  const onDrop = useCallback((e: React.DragEvent) => {
    const raw = e.dataTransfer.getData(DRAG_MIME);
    if (!raw) return;
    e.preventDefault();
    const item = JSON.parse(raw) as CatalogItem;
    const type = item.type;
    const p = flow.screenToFlowPosition({ x: e.clientX, y: e.clientY });
    const generic = item.vendor === "generic";
    send([{ op: "add_node", node: {
      name: type === "note" ? "Note" : generic ? `New ${TYPE_META[type].label}` : item.name, type,
      technology: generic ? "" : item.name, vendor: generic ? "" : item.vendor, catalog_id: generic ? "" : item.id,
      description: type === "note" ? "Select it and edit the text in the inspector" : "",
      position: { x: Math.round(p.x - 90), y: Math.round(p.y - 30) },
    } }]);
  }, [flow, send]);

  const lastCursor = useRef(0);
  const onMouseMove = useCallback((e: React.MouseEvent) => {
    const now = performance.now();
    if (now - lastCursor.current < 50) return;
    lastCursor.current = now;
    const p = flow.screenToFlowPosition({ x: e.clientX, y: e.clientY });
    setCursor({ x: p.x, y: p.y });
  }, [flow, setCursor]);

  // ---- copy / paste / duplicate ----
  const copySelection = useCallback(() => {
    const s = stateRef.current;
    if (!s || !selectedNodeIds.length) return false;
    const ids = new Set(selectedNodeIds);
    clipboard.current = {
      nodes: selectedNodeIds.map((id) => s.nodes[id]).filter(Boolean),
      edges: Object.values(s.edges).filter((e) => ids.has(e.source) && ids.has(e.target))
        .map(({ source, target, protocol, label }) => ({ source, target, protocol, label })),
    };
    return true;
  }, [selectedNodeIds]);

  const paste = useCallback(() => {
    const clip = clipboard.current;
    if (!clip) return;
    const idMap: Record<string, string> = {};
    const ops: Op[] = clip.nodes.map((n) => {
      idMap[n.id] = `${n.id}-${rand()}`;
      return { op: "add_node", node: {
        id: idMap[n.id], name: n.name, type: n.type, technology: n.technology, description: n.description,
        requirement_ids: n.requirement_ids, props: n.props, position: { x: n.position.x + 60, y: n.position.y + 60 } } };
    });
    for (const e of clip.edges) ops.push({ op: "add_edge", edge: { ...e, source: idMap[e.source], target: idMap[e.target] } });
    send(ops);
    // next paste lands further offset
    clipboard.current = { ...clip, nodes: clip.nodes.map((n) => ({ ...n, position: { x: n.position.x + 60, y: n.position.y + 60 } })) };
  }, [send]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t.closest("input, textarea, select, [contenteditable]")) return;
      const mod = e.ctrlKey || e.metaKey;
      if (mod && e.key === "c") { if (copySelection()) e.preventDefault(); }
      else if (mod && e.key === "v") { e.preventDefault(); paste(); }
      else if (mod && e.key === "d") { e.preventDefault(); if (copySelection()) paste(); }
      else if (mod && !e.shiftKey && e.key.toLowerCase() === "z") { e.preventDefault(); undo(); }
      else if (mod && (e.key.toLowerCase() === "y" || (e.shiftKey && e.key.toLowerCase() === "z"))) { e.preventDefault(); redo(); }
      else if (e.key === "Escape") { setTraced(null); setSearch(""); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [copySelection, paste]);

  // ---- AI ----
  const logId = useRef(0);
  const updateLog = (id: number, patch: Partial<LogEntry>) => setLog((l) => l.map((e) => (e.id === id ? { ...e, ...patch } : e)));

  const runCommand = async (text: string) => {
    const id = ++logId.current;
    setLog((l) => [...l, { id, kind: "command", text, pending: true }]);
    try {
      const r = await api.command(pid, branch, text, selectedNodeIds, user.name);
      updateLog(id, { pending: false, explanation: r.explanation, summary: r.summary, questions: r.questions,
        engine: r.engine, error: r.applied ? undefined : r.error });
    } catch (e) {
      updateLog(id, { pending: false, error: (e as Error).message });
    }
  };

  const runAsk = async (question: string) => {
    const id = ++logId.current;
    setLog((l) => [...l, { id, kind: "ask", text: question, pending: true }]);
    try {
      const r = await api.ask(pid, branch, question);
      updateLog(id, { pending: false, answer: r.answer });
    } catch (e) {
      updateLog(id, { pending: false, error: (e as Error).message });
    }
  };

  // ---- versions ----
  const commit = async (message: string) => {
    try {
      const v = await api.commit(pid, branch, message, user.name);
      toast(`Committed v${v.number}`);
      refreshMeta();
    } catch (e) { toast(`⚠ ${(e as Error).message}`); }
  };
  const showDiff = async (v: number | null) => {
    if (v === null) { setDiff(null); return; }
    try { setDiff({ from: v, data: await api.diff(pid, branch, String(v)) }); }
    catch (e) { toast(`⚠ ${(e as Error).message}`); }
  };
  const restore = async (v: number) => {
    try {
      await api.restore(pid, branch, v, user.name);
      setDiff(null);
      toast(`Restored v${v} into ${branch} (commit to keep it as a version)`);
    } catch (e) { toast(`⚠ ${(e as Error).message}`); }
  };
  const createBranch = async (name: string, fromVersion: number | null) => {
    try {
      await api.createBranch(pid, name, branch, fromVersion);
      onBranch(name);
    } catch (e) { toast(`⚠ ${(e as Error).message}`); }
  };

  const focusIssue = (i: Issue) => {
    if (i.requirement_ids[0]) { setTraced(i.requirement_ids[0]); setTab("requirements"); }
    if (i.node_ids.length) {
      const ids = new Set(i.node_ids);
      setNodes((ns) => ns.map((n) => ({ ...n, selected: ids.has(n.id) })));
      flow.fitView({ nodes: i.node_ids.map((id) => ({ id })), padding: 0.6, duration: 400, maxZoom: 1.2 });
    }
  };

  const onSearchEnter = () => {
    if (highlight?.size) flow.fitView({ nodes: [...highlight].map((id) => ({ id })), padding: 0.5, duration: 400, maxZoom: 1.2 });
  };

  if (loadError) {
    return <div className="picker"><p className="error">{loadError}</p><button onClick={onExit}>Back to projects</button></div>;
  }

  const others = live.peers.filter((p) => p.clientId !== live.selfId);
  const aiBusy = rt.aiStatus.status === "thinking" ? `AI is applying “${rt.aiStatus.text}” for ${rt.aiStatus.actor}` : null;

  return (
    <div className="workspace">
      <header className="topbar">
        <button className="ghost brand-btn" onClick={onExit} title="All projects"><span className="logo">▲</span></button>
        <div className="title">{project?.name ?? "…"}</div>
        <div className="branch">
          <select value={branch} onChange={(e) => onBranch(e.target.value)} aria-label="Branch">
            {branches.map((b) => <option key={b.name} value={b.name}>⎇ {b.name}</option>)}
          </select>
          {newBranch === null ? (
            <button className="small" onClick={() => setNewBranch("experiment/")}>New branch</button>
          ) : (
            <form className="row gap" onSubmit={(e) => { e.preventDefault(); createBranch(newBranch.trim(), null); setNewBranch(null); }}>
              <input autoFocus value={newBranch} onChange={(e) => setNewBranch(e.target.value)} aria-label="New branch name" />
              <button className="small primary" disabled={!/^[A-Za-z0-9._/-]{1,60}$/.test(newBranch.trim())}>Create</button>
              <button type="button" className="small" onClick={() => setNewBranch(null)}>✕</button>
            </form>
          )}
        </div>
        <input className="search" placeholder="Search canvas…" value={search}
          onChange={(e) => { setSearch(e.target.value); setTraced(null); }}
          onKeyDown={(e) => e.key === "Enter" && onSearchEnter()} />
        <div className="spacer" />
        {analysis && (
          <button className="health-pill" onClick={() => setTab("issues")} title="Architecture health">
            {analysis.health.issues.errors > 0 && <span className="dot error" />}
            {analysis.health.issues.warnings > 0 && <span className="dot warning" />}
            {analysis.health.issues.errors + analysis.health.issues.warnings} issues · trace {analysis.health.traceability}%
          </button>
        )}
        {ai && (
          <span className={`ai-pill ${ai.available ? "on" : "off"}`}
            title={ai.available ? `AI model: ${ai.model}` : "No LLM key configured: the design chat and commands use built-in templates. Add GEMINI_API_KEY to backend/.env and restart the API."}>
            {ai.available ? "AI on" : "AI off · templates"}
          </span>
        )}
        <select className="convert" value="" disabled={converting} aria-label="Convert architecture to another platform"
          title="Swap every recognised component for its equivalent on another platform"
          onChange={async (e) => {
            const target = e.target.value;
            if (!target) return;
            setConverting(true);
            try {
              const r = await api.convert(pid, branch, target, user.name);
              toast(r.changed ? `Converted ${r.changed} component(s) to ${catalog?.vendors[target] ?? target}` : "Nothing to convert");
              if (r.skipped.length) toast(`No equivalent for: ${r.skipped.join(", ")}`);
            } catch (err) { toast(`⚠ ${(err as Error).message}`); }
            finally { setConverting(false); }
          }}>
          <option value="">{converting ? "Converting…" : "⇄ Convert to…"}</option>
          <option value="aws">AWS</option><option value="azure">Azure</option>
          <option value="gcp">Google Cloud</option><option value="oss">Open source (self-hosted)</option>
        </select>
        <button className="small" onClick={() => setConnectOpen(true)} title="Connect Claude, Codex or other agents over MCP">⌘ Connect AI</button>
        {themeToggle}
        <div className="presence">
          <span className={`conn ${live.status === "connected" && live.synced ? "on" : ""}`}
            title={live.status === "connected" ? (live.synced ? "Live: changes sync instantly" : "Syncing…") : "Reconnecting… your edits are kept and sync when back online"} />
          <span className="avatar" style={{ background: user.color }} title={`${user.name} (you)`}>{user.name[0]?.toUpperCase()}</span>
          {others.map((u) => <span key={u.clientId} className="avatar" style={{ background: u.color }} title={u.name}>{u.name[0]?.toUpperCase()}</span>)}
        </div>
        <button className="primary" onClick={() => setPromptFor({ nodeIds: selectedNodeIds, requirementIds: [] })}>
          ⚙ Vibe code{selectedNodeIds.length ? ` (${selectedNodeIds.length})` : ""}
        </button>
      </header>

      <aside className="sidebar">
        <nav className="tabs">
          {(["design", "requirements", "issues", "versions"] as Tab[]).map((t) => (
            <button key={t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>
              {t === "design" ? "Design" : t === "requirements" ? `Requirements${state ? ` (${Object.keys(state.requirements).length})` : ""}`
                : t === "issues" ? `Health${analysis ? ` (${analysis.health.issues.errors + analysis.health.issues.warnings})` : ""}` : "Versions"}
            </button>
          ))}
        </nav>
        {state && tab === "design" && (
          <DesignChat pid={pid} branch={branch} user={user} aiAvailable={ai?.available ?? true} canvasEmpty={Object.keys(state.nodes).length === 0}
            onApplied={(r) => {
              if (r.branch !== branch) { onBranch(r.branch); return; }
              refreshMeta();
              toast(`Applied architecture · v${r.version}`);
              window.setTimeout(() => flow.fitView({ padding: 0.15, duration: 400 }), 400); // after the live document receives the change
            }} />
        )}
        {state && tab === "requirements" && (
          <RequirementsPanel state={state} issues={analysis?.requirement_issues ?? []} traced={traced} onTrace={setTraced}
            send={send} onPrompt={(rid) => setPromptFor({ nodeIds: [], requirementIds: [rid] })} />
        )}
        {tab === "issues" && <IssuesPanel analysis={analysis} onFocus={focusIssue} onAsk={runAsk} />}
        {tab === "versions" && (
          <VersionsPanel versions={versions} activity={activity} branch={branch} headVersion={headVersion} diff={diff}
            onCommit={commit} onDiff={showDiff} onRestore={restore} onBranchFrom={(v, name) => createBranch(name, v)} />
        )}
      </aside>

      <main className="canvas" ref={wrapper} onMouseMove={onMouseMove}
        onDragOver={(e) => { e.preventDefault(); e.dataTransfer.dropEffect = "move"; }} onDrop={onDrop}>
        <ReactFlow
          nodes={nodes} edges={edges} nodeTypes={nodeTypes}
          onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onDelete={onDelete}
          onNodeDragStop={onNodeDragStop} onConnect={onConnect}
          deleteKeyCode={["Delete", "Backspace"]} multiSelectionKeyCode={["Shift", "Meta", "Control"]}
          selectionKeyCode="Shift" colorMode={colorMode} fitView fitViewOptions={{ padding: 0.15 }} minZoom={0.1} maxZoom={2.5} proOptions={{ hideAttribution: true }}
        >
          <Background variant={BackgroundVariant.Dots} gap={20} size={1} />
          <Controls position="center-right" />
          <MiniMap position="top-right" pannable zoomable nodeColor={(n) => TYPE_META[(n.data as { model: ArchNode }).model.type]?.color ?? "#999"} />
          <ViewportPortal>
            {others.filter((c) => c.cursor).map((c) => (
              <div key={c.clientId} className="remote-cursor" style={{ transform: `translate(${c.cursor!.x}px, ${c.cursor!.y}px)`, color: c.color }}>
                <svg width="16" height="16" viewBox="0 0 16 16"><path d="M1 1 L6 15 L8 9 L14 7 Z" fill="currentColor" stroke="white" strokeWidth="1" /></svg>
                <span style={{ background: c.color }}>{c.name}</span>
              </div>
            ))}
          </ViewportPortal>
        </ReactFlow>
        <Palette catalog={catalog} />
        {diff && <div className="diff-banner">Diff: v{diff.from} → working · {diff.data.total_changes} change(s) <button className="small" onClick={() => setDiff(null)}>Exit</button></div>}
        {traced && <div className="diff-banner">Tracing {traced} <button className="small" onClick={() => setTraced(null)}>Clear</button></div>}
        {state && Object.keys(state.nodes).length === 0 && (
          <div className="empty-canvas">
            <h3>Empty canvas</h3>
            <p className="muted">Drag components from the palette, or describe your system below — e.g.
              “Build an AI customer-support platform where customers use WhatsApp and web.”</p>
          </div>
        )}
        <CommandBar log={log} selectionCount={selectedNodeIds.length} aiBusy={aiBusy} onCommand={runCommand} onAsk={runAsk} />
        <div className="toasts">{toasts.map((t) => <div key={t.id} className="toast">{t.text}</div>)}</div>
      </main>

      <aside className="rightbar">
        {state && (
          <Inspector catalog={catalog} state={state} nodeIds={selectedNodeIds} edgeIds={selectedEdgeIds} send={send}
            onPrompt={() => setPromptFor({ nodeIds: selectedNodeIds, requirementIds: [] })} />
        )}
      </aside>

      {connectOpen && project && (
        <ConnectDialog orgId={project.org_id ?? "org-default"} user={user} onClose={() => setConnectOpen(false)} />
      )}
      {promptFor && <PromptDialog pid={pid} branch={branch} nodeIds={promptFor.nodeIds} requirementIds={promptFor.requirementIds}
        onClose={() => setPromptFor(null)} />}
    </div>
  );
}
