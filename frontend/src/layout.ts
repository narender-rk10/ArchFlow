import type { NodeType } from "./types";

/**
 * Auto-arrange a diagram level.
 *
 * System level ("tiers"): one band per architectural tier, top to bottom: clients → gateways → services, agents
 * and security → caches and queues → databases and storage → external systems and LLMs.
 * Internals ("flow"): bands follow the call flow along connections (endpoint → controller → service →
 * repository → table/event), by longest path.
 *
 * In each band, components are ordered to reduce crossing lines (barycenter sweeps, starting from the current
 * left-to-right order so the picture stays familiar), placed under the components they connect to, and wide bands
 * wrap onto extra rows. Notes keep their position.
 */

export interface LayoutNode { id: string; type: NodeType; x: number; y: number; width: number; height: number }
export interface LayoutEdge { source: string; target: string }

const TIER: Partial<Record<NodeType, number>> = {
  client: 0, gateway: 1, service: 2, agent: 2, mcp: 2, security: 2, component: 2,
  cache: 3, queue: 3, event: 3, database: 4, storage: 4, table: 4, external: 5, llm: 5, endpoint: 0,
};

const GAP_X = 56;        // between neighbours in a row
const GAP_BAND = 110;    // between tiers (room for connection labels)
const GAP_WRAP = 48;     // between wrapped rows of the same tier
const MAX_ROW_WIDTH = 2100; // about 8 components before a tier wraps

function flowRanks(nodes: LayoutNode[], edges: LayoutEdge[]): Map<string, number> {
  const ids = new Set(nodes.map((n) => n.id));
  const out = new Map<string, string[]>(nodes.map((n) => [n.id, []]));
  for (const e of edges) if (ids.has(e.source) && ids.has(e.target) && e.source !== e.target) out.get(e.source)!.push(e.target);
  // Drop back edges (cycles) with a DFS, then longest path from the sources.
  const state = new Map<string, 0 | 1 | 2>();
  const dag = new Map<string, string[]>(nodes.map((n) => [n.id, []]));
  const visit = (v: string) => {
    state.set(v, 1);
    for (const w of out.get(v)!) {
      if (state.get(w) === 1) continue; // back edge
      dag.get(v)!.push(w);
      if (!state.get(w)) visit(w);
    }
    state.set(v, 2);
  };
  const order = [...nodes].sort((a, b) => (TIER[a.type] ?? 2) - (TIER[b.type] ?? 2) || a.y - b.y);
  for (const n of order) if (!state.get(n.id)) visit(n.id);
  const rank = new Map<string, number>();
  const indeg = new Map<string, number>(nodes.map((n) => [n.id, 0]));
  for (const ws of dag.values()) for (const w of ws) indeg.set(w, indeg.get(w)! + 1);
  const queue = order.filter((n) => indeg.get(n.id) === 0).map((n) => n.id);
  for (const id of queue) rank.set(id, 0);
  while (queue.length) {
    const v = queue.shift()!;
    for (const w of dag.get(v)!) {
      rank.set(w, Math.max(rank.get(w) ?? 0, rank.get(v)! + 1));
      indeg.set(w, indeg.get(w)! - 1);
      if (indeg.get(w) === 0) queue.push(w);
    }
  }
  // Unconnected elements go to the band their type belongs to (tables and events last).
  const maxRank = Math.max(0, ...rank.values());
  for (const n of nodes) {
    const linked = edges.some((e) => e.source === n.id || e.target === n.id);
    if (!linked) rank.set(n.id, n.type === "table" || n.type === "event" ? maxRank + 1 : Math.min(TIER[n.type] ?? 1, maxRank));
  }
  return rank;
}

export function autoLayout(nodes: LayoutNode[], edges: LayoutEdge[], mode: "tiers" | "flow"): Map<string, { x: number; y: number }> {
  const result = new Map<string, { x: number; y: number }>();
  if (!nodes.length) return result;
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const adj = new Map<string, string[]>(nodes.map((n) => [n.id, []]));
  for (const e of edges) {
    if (byId.has(e.source) && byId.has(e.target) && e.source !== e.target) {
      adj.get(e.source)!.push(e.target);
      adj.get(e.target)!.push(e.source);
    }
  }
  const rank = mode === "flow" ? flowRanks(nodes, edges) : new Map(nodes.map((n) => [n.id, TIER[n.type] ?? 2]));
  const ranks = [...new Set(rank.values())].sort((a, b) => a - b);
  const bands = ranks.map((r) => nodes.filter((n) => rank.get(n.id) === r).sort((a, b) => a.x - b.x || a.y - b.y).map((n) => n.id));

  // Crossing reduction: order each band by the mean relative position of its neighbours in the other bands.
  const pos = new Map<string, number>();
  const index = () => bands.forEach((band) => band.forEach((id, i) => pos.set(id, band.length > 1 ? i / (band.length - 1) : 0.5)));
  index();
  const sweep = (bandIdx: number[], dir: "down" | "up") => {
    for (const b of bandIdx) {
      const here = rank.get(bands[b][0])!;
      const key = new Map<string, number>();
      bands[b].forEach((id) => {
        const ns = adj.get(id)!.filter((w) => (dir === "down" ? rank.get(w)! < here : rank.get(w)! > here));
        key.set(id, ns.length ? ns.reduce((s, w) => s + pos.get(w)!, 0) / ns.length : pos.get(id)!);
      });
      bands[b].sort((a, c) => key.get(a)! - key.get(c)!);
      bands[b].forEach((id, i) => pos.set(id, bands[b].length > 1 ? i / (bands[b].length - 1) : 0.5));
    }
  };
  const all = bands.map((_, i) => i);
  for (let k = 0; k < 4; k++) { sweep(all.slice(1), "down"); sweep(all.slice(0, -1).reverse(), "up"); }

  // Place rows top to bottom; each component as close as possible under the ones it connects to.
  const placed = new Map<string, { x: number; y: number }>();
  let y = 0;
  const centre = (id: string) => placed.get(id)!.x + byId.get(id)!.width / 2;
  bands.forEach((band, bi) => {
    // Once the tiers above are placed, order this one by where its neighbours are, so a service's own database
    // lands under the service (left-to-right reading order matches the tier above).
    if (bi > 0) {
      const want = new Map(band.map((id) => {
        const ns = adj.get(id)!.filter((w) => placed.has(w));
        return [id, ns.length ? ns.reduce((s, w) => s + centre(w), 0) / ns.length : NaN] as const;
      }));
      if ([...want.values()].filter((v) => !Number.isNaN(v)).length >= band.length / 2) {
        const known = band.filter((id) => !Number.isNaN(want.get(id)!)).sort((a, b) => want.get(a)! - want.get(b)!);
        const loose = band.filter((id) => Number.isNaN(want.get(id)!));
        band.splice(0, band.length, ...known, ...loose);
      }
    }
    // A crowded tier wraps by interleaving (1st, 3rd, 5th… on one row; 2nd, 4th… on the next), so every row spans
    // the tier's full width and each component can still sit under the ones it connects to.
    const bandWidth = band.reduce((sum, id) => sum + byId.get(id)!.width + GAP_X, 0);
    const rowCount = Math.max(1, Math.ceil(bandWidth / MAX_ROW_WIDTH));
    const rows: string[][] = Array.from({ length: rowCount }, () => []);
    band.forEach((id, i) => rows[i % rowCount].push(id));
    rows.forEach((row, ri) => {
      const desired = row.map((id) => {
        const ns = adj.get(id)!.filter((w) => placed.has(w));
        const n = byId.get(id)!;
        return ns.length ? ns.reduce((s, w) => s + placed.get(w)!.x + byId.get(w)!.width / 2, 0) / ns.length - n.width / 2 : NaN;
      });
      const total = row.reduce((s, id) => s + byId.get(id)!.width, 0) + GAP_X * (row.length - 1);
      // Start from an evenly spaced row centred on 0, pull each box towards its neighbours, keep the gaps.
      const xs: number[] = [];
      let cursor = -total / 2;
      row.forEach((id, i) => {
        const w = byId.get(id)!.width;
        const target = Number.isNaN(desired[i]) ? cursor : desired[i];
        xs.push(Math.max(cursor, target));
        cursor = xs[i] + w + GAP_X;
      });
      // Re-centre the row on the neighbours it follows (or on 0 when it follows none).
      const known = desired.map((d, i) => (Number.isNaN(d) ? NaN : d - xs[i])).filter((d) => !Number.isNaN(d));
      const shift = known.length ? known.reduce((s, d) => s + d, 0) / known.length
        : -(xs[0] + (xs[xs.length - 1] + byId.get(row[row.length - 1])!.width)) / 2;
      const rowHeight = Math.max(...row.map((id) => byId.get(id)!.height));
      row.forEach((id, i) => placed.set(id, { x: xs[i] + shift, y }));
      y += rowHeight + (ri < rows.length - 1 ? GAP_WRAP : 0);
    });
    if (bi < bands.length - 1) y += GAP_BAND;
  });

  // Keep the diagram where it was: align the new bounding box's top-left with the old one.
  const oldX = Math.min(...nodes.map((n) => n.x));
  const oldY = Math.min(...nodes.map((n) => n.y));
  const newX = Math.min(...[...placed.values()].map((p) => p.x));
  const newY = Math.min(...[...placed.values()].map((p) => p.y));
  for (const [id, p] of placed) result.set(id, { x: Math.round(p.x - newX + oldX), y: Math.round(p.y - newY + oldY) });
  return result;
}
