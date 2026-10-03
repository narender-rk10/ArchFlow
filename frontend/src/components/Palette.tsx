import { useMemo, useState } from "react";
import type { Catalog, CatalogItem } from "../types";
import { TYPE_META } from "./ArchNode";

export const DRAG_MIME = "application/archflow-catalog";

export const VENDOR_STYLE: Record<string, { short: string; color: string }> = {
  aws: { short: "AWS", color: "#ff9900" },
  azure: { short: "Azure", color: "#0078d4" },
  gcp: { short: "GCP", color: "#34a853" },
  oss: { short: "OSS", color: "#6b7280" },
  saas: { short: "SaaS", color: "#8e4ec6" },
};

const VENDOR_ORDER = ["all", "generic", "aws", "azure", "gcp", "oss", "saas"];

/** Searchable component catalog: generic building blocks plus concrete AWS/Azure/GCP/OSS/SaaS services. */
export function Palette({ catalog }: { catalog: Catalog | null }) {
  const [query, setQuery] = useState("");
  const [vendor, setVendor] = useState("all");
  const [collapsed, setCollapsed] = useState(false);

  const groups = useMemo(() => {
    const q = query.trim().toLowerCase();
    const items = (catalog?.items ?? []).filter((c) =>
      (vendor === "all" ? c.vendor === "generic" || q : c.vendor === vendor) &&
      (!q || `${c.name} ${c.kind_label} ${c.category} ${catalog?.vendors[c.vendor] ?? ""}`.toLowerCase().includes(q)));
    const out = new Map<string, CatalogItem[]>();
    for (const c of items) out.set(c.category, [...(out.get(c.category) ?? []), c]);
    return [...out.entries()];
  }, [catalog, query, vendor]);

  const onDragStart = (e: React.DragEvent, item: CatalogItem) => {
    e.dataTransfer.setData(DRAG_MIME, JSON.stringify(item));
    e.dataTransfer.effectAllowed = "move";
  };

  if (collapsed) {
    return <button className="palette-toggle" onClick={() => setCollapsed(false)} title="Show components">＋ Components</button>;
  }
  return (
    <div className="palette" aria-label="Component catalog">
      <div className="palette-head">
        <input placeholder="Search 140+ components…" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search components" />
        <button className="ghost small" onClick={() => setCollapsed(true)} aria-label="Hide components" title="Hide">⟨</button>
      </div>
      <div className="vendor-chips" role="tablist" aria-label="Vendor">
        {VENDOR_ORDER.map((v) => (
          <button key={v} role="tab" aria-selected={vendor === v} className={`vchip ${vendor === v ? "on" : ""}`}
            onClick={() => setVendor(v)}>
            {v === "all" ? "All" : v === "generic" ? "Generic" : VENDOR_STYLE[v]?.short ?? v}
          </button>
        ))}
      </div>
      <div className="palette-list">
        {groups.length === 0 && <p className="muted tiny pad">No matches.</p>}
        {groups.map(([category, items]) => (
          <div key={category}>
            <div className="palette-cat">{category}</div>
            {items.map((c) => (
              <div key={c.id} className="palette-item" draggable onDragStart={(e) => onDragStart(e, c)}
                title={c.kind_label ? `${c.name} · ${c.kind_label}` : `Drag a ${c.name} onto the canvas`}
                style={{ ["--accent" as string]: TYPE_META[c.type].color }}>
                <span className="an-glyph">{TYPE_META[c.type].glyph}</span>
                <span className="pi-name">{c.name}</span>
                {VENDOR_STYLE[c.vendor] && (
                  <span className="vbadge" style={{ ["--vc" as string]: VENDOR_STYLE[c.vendor].color }}>{VENDOR_STYLE[c.vendor].short}</span>
                )}
              </div>
            ))}
          </div>
        ))}
        {vendor === "all" && !query && <p className="muted tiny pad">Pick a vendor or search to see cloud services.</p>}
      </div>
    </div>
  );
}
