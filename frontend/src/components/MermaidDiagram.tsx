import { useEffect, useId, useState } from "react";

type MermaidApi = typeof import("mermaid").default;
let loading: Promise<MermaidApi> | null = null;

/** Mermaid is large, so it is loaded the first time a diagram is shown. */
function loadMermaid(): Promise<MermaidApi> {
  loading ??= import("mermaid").then((m) => m.default);
  return loading;
}

function prefersDark(): boolean {
  const theme = document.documentElement.dataset.theme;
  return theme ? theme === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
}

/** Draws Mermaid source (sequence and ER diagrams from the low-level design). */
export function MermaidDiagram({ source, label }: { source: string; label: string }) {
  const id = `mmd-${useId().replace(/[^a-zA-Z0-9]/g, "")}`;
  const [svg, setSvg] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setError("");
    loadMermaid().then(async (mermaid) => {
      // "strict" escapes labels and disables click handlers, so names from the model cannot inject markup.
      mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: prefersDark() ? "dark" : "default",
        fontFamily: "Inter, system-ui, sans-serif" });
      const { svg: out } = await mermaid.render(id, source);
      if (!cancelled) setSvg(out);
    }).catch((e) => { if (!cancelled) { setSvg(""); setError((e as Error).message || "Could not draw this diagram."); } });
    return () => { cancelled = true; };
  }, [id, source]);

  if (error) return <p className="error small">Could not draw the diagram: {error}</p>;
  if (!svg) return <p className="muted small pulse">Drawing…</p>;
  return <div className="mermaid-view" role="img" aria-label={label} dangerouslySetInnerHTML={{ __html: svg }} />;
}
