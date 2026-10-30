// Parses Mermaid sources with the frontend's own Mermaid (used by test_diagrams.py).
// Usage: node mermaid_parse.mjs <frontend dir>  < sources.json   →  prints [{ok, type?, error?}, ...]
// Rendering needs a browser, but parsing only needs DOMPurify's hooks, which are stubbed here.
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const root = process.argv[2];
const load = (rel) => import(pathToFileURL(`${root}/node_modules/${rel}`).href).then((m) => m.default);
const purify = await load("dompurify/dist/purify.es.mjs");
Object.assign(purify, { addHook() {}, removeHook() {}, removeHooks() {}, removeAllHooks() {}, sanitize: (s) => String(s) });
const mermaid = await load("mermaid/dist/mermaid.core.mjs");
mermaid.initialize({ startOnLoad: false, securityLevel: "strict" });

const out = [];
for (const source of JSON.parse(readFileSync(0, "utf8"))) {
  try {
    out.push({ ok: true, type: (await mermaid.parse(source)).diagramType });
  } catch (e) {
    out.push({ ok: false, error: String(e?.message ?? e) });
  }
}
process.stdout.write(JSON.stringify(out));
