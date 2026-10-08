# ArchFlow renderer

ArchFlow's diagram renderer: it turns a typed JSON description (architecture, workflow, sequence, data flow,
lifecycle) into a standalone, ArchFlow-branded interactive HTML viewer: node exploration, path tracing, themes,
PNG / JPEG / WebP / SVG / WebM export. The backend calls it from `archflow/diagram_export.py`:

    node renderer/bin/archflow.mjs render <type> <input.json> <output.html> --quality standard

It runs on the API server only (Node.js 18+, no npm dependencies, no network access).

## Origin and licence

Forked on 2026-10-07 from [Archify](https://github.com/tt-a1i/archify) 3.0.1, commit
`73aaa0696e8f72c232ea710e6fa94fd953f3e773` (directory `archify/`), which is MIT-licensed. The original copyright
notice and licence are kept in [`LICENSE`](LICENSE), as the MIT licence requires. Third-party logo data and its
terms are listed in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

ArchFlow changes:

- Renamed to ArchFlow throughout (names, identifiers, files: `bin/archflow.mjs`, schema ids under
  `https://archflow.local/schemas/`), package `archflow-renderer` 1.0.0.
- ArchFlow brand in the viewer (`assets/template.html`, block `archflow-brand`): ▲ logo in the header, indigo
  accent, page icon, "Made with ArchFlow" credit, generator tag.
- The update check against the upstream project is switched off (`bin/delivery-update.mjs`).
- Only the runtime was taken: `bin/`, `renderers/`, `schemas/`, `assets/`, `brand-marks/`, `delta/`, `recipes/`,
  `migrations/`, `references/`. Upstream tests, examples, docs and build scripts were not.

ArchFlow maintains this copy from here on; there is no automatic sync with upstream.
