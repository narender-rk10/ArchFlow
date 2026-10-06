"""``archflow-codegraph <folder> [-o out.json]`` — analyse a repository on your own machine.

Only the resulting normalized graph (file paths, symbol names, relations, detected infrastructure) is written;
upload it in ArchFlow (Code → Upload graph, or POST /api/projects/{pid}/codegraph/upload). Source code never
leaves your machine. Needs the ``codegraph`` extra: ``uv sync --extra codegraph`` (or ``pip install
"archflow[codegraph]"``).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="archflow-codegraph", description=__doc__.split("\n\n")[0])
    ap.add_argument("folder", help="repository root to analyse")
    ap.add_argument("-o", "--output", default="archflow-codegraph.json", help="output file (default: %(default)s)")
    args = ap.parse_args(argv)
    root = Path(args.folder)
    if not root.is_dir():
        ap.error(f"not a directory: {root}")
    try:
        from .adapter import analyze_directory
    except ImportError:
        print("Graphify is not installed. Run: uv sync --extra codegraph", file=sys.stderr)
        return 3
    from .normalize import summarize

    with tempfile.TemporaryDirectory(prefix="archflow-graphify-cache-") as cache:
        graph = analyze_directory(root, Path(cache))
    Path(args.output).write_text(json.dumps(graph, separators=(",", ":")), encoding="utf-8")
    s = summarize(graph)
    st = s["stats"]
    print(f"Wrote {args.output}: {st['files']} files, {st['symbols']} symbols, {st['edges']} relations, "
          f"{st['communities']} communities")
    if s["infrastructure"]:
        print("Detected: " + ", ".join(i["label"] for i in s["infrastructure"]))
    print("Upload it in ArchFlow: Code → Upload graph. No source code is included.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
