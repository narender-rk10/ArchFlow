"""Analysis worker process: ``python -m archflow.codegraph.worker <repo_dir> <out.json>``.

The API never parses repositories in its own process: it starts this module in a subprocess with a timeout
(``ARCHFLOW_CODEGRAPH_TIMEOUT``, default 600 s), so a pathological repository can only exhaust a short-lived
worker. Exit codes: 0 ok, 2 bad arguments, 3 Graphify not installed, 1 analysis failed (message on stderr).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: python -m archflow.codegraph.worker <repo_dir> <out.json>", file=sys.stderr)
        return 2
    repo, out = Path(args[0]), Path(args[1])
    if not repo.is_dir():
        print(f"not a directory: {repo}", file=sys.stderr)
        return 2
    try:
        from .adapter import analyze_directory
    except ImportError as exc:
        print(f"Graphify is not installed ({exc}). Install with: uv sync --extra codegraph", file=sys.stderr)
        return 3
    os.environ.setdefault("GRAPHIFY_MAX_WORKERS", os.environ.get("ARCHFLOW_CODEGRAPH_PROCS", "2"))
    try:
        with tempfile.TemporaryDirectory(prefix="archflow-graphify-cache-") as cache:
            graph = analyze_directory(repo, Path(cache))
        tmp = out.with_suffix(out.suffix + ".part")
        tmp.write_text(json.dumps(graph, separators=(",", ":")), encoding="utf-8")
        tmp.replace(out)
    except Exception as exc:  # report and fail; the API turns this into a failed job
        print(f"analysis failed: {type(exc).__name__}: {exc}"[:2000], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
