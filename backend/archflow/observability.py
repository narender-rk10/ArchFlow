"""AI observability: where agent traces are stored, and the admin API that reads them and the prompt catalog.

Every agent run (``agents.runtime``) produces a trace (``agents.tracing``): input, output, each model request
(model, tokens, cost, latency) and each tool call (arguments and result). ``install`` stores them in the
``ai_traces`` table and prunes old ones; organization admins read them under AI settings → Traces.

Environment:
    ARCHFLOW_TRACE_RETENTION_DAYS   keep traces this long (default 30)
    ARCHFLOW_TRACES                 full | metadata | off (see agents.tracing)
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException

from .agents import prompts, tracing

log = logging.getLogger("archflow.observability")

PRUNE_EVERY = 200  # traces saved between retention sweeps


def _retention_days() -> int:
    try:
        return max(1, int(os.environ.get("ARCHFLOW_TRACE_RETENTION_DAYS", "30")))
    except ValueError:
        return 30


def install(store: Any) -> None:
    """Send finished traces to ``store`` and export OpenTelemetry spans when configured."""
    lock = threading.Lock()
    count = {"n": 0}

    def sink(trace: dict[str, Any]) -> None:
        if not trace.get("org_id"):
            return  # calls outside an organization (CLI, tests without accounts) are not stored
        store.save_ai_trace(trace)
        with lock:
            count["n"] += 1
            due = count["n"] % PRUNE_EVERY == 1
        if due:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=_retention_days())).isoformat()
            try:
                store.prune_ai_traces(cutoff)
            except Exception:  # noqa: BLE001
                log.exception("pruning AI traces failed")

    tracing.set_sink(sink)
    tracing.setup_otel()


def register_routes(app: FastAPI, store: Any, authz: Any) -> None:
    admin = authz.org("admin")

    @app.get("/api/orgs/{org_id}/ai/traces")
    def list_traces(org_id: str, project_id: str | None = None, feature: str | None = None,
                    prompt: str | None = None, status: str | None = None, user_id: str | None = None,
                    before: str | None = None, limit: int = 50, all_runs: bool = False, a=Depends(admin)):
        """Agent runs, newest first: who asked what, what the AI answered, latency, tokens and cost. Runs fired by
        another run (agents in parallel) are left out unless ``all_runs``; they appear under their parent."""
        projects = {p["id"]: p["name"] for p in store.list_projects(org_id)}
        rows = store.list_ai_traces(org_id, limit=limit, before=before, top_level=not all_runs,
                                    project_id=project_id, feature=feature, prompt=prompt, status=status,
                                    user_id=user_id)
        people = {m["user_id"]: m["name"] for m in store.list_members(org_id)}
        return {"traces": [{**r, "project_name": projects.get(r["project_id"], "")} for r in rows],
                "people": [{"id": k, "name": v} for k, v in sorted(people.items(), key=lambda kv: kv[1].lower())],
                "content": tracing.mode(), "retention_days": _retention_days()}

    @app.get("/api/orgs/{org_id}/ai/traces/{trace_id}")
    def get_trace(org_id: str, trace_id: str, a=Depends(admin)):
        t = store.get_ai_trace(org_id, trace_id)
        if not t:
            raise HTTPException(404, "trace not found")
        return t

    @app.get("/api/orgs/{org_id}/ai/prompts")
    def prompt_catalog(org_id: str, a=Depends(admin)):
        return {"prompts": prompts.catalog(), "problems": prompts.lock_problems()}
