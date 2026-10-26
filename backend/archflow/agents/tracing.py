"""Traces of every agent run: input, output, each model call (model, tokens, cost, latency) and each tool call.

A trace is opened by ``agents.runtime`` around one agent run. Model calls made inside it are reported by the
gateway (``llm.observe_calls``), so their tokens and cost are the same numbers the AI-usage table records;
tool calls are read from the run's messages afterwards. Finished traces go to a sink set by
``main.create_app`` (the store's ``ai_traces`` table) and, when OpenTelemetry is configured, Pydantic AI's own
spans are exported too (``setup_otel``).

Environment:
    ARCHFLOW_TRACES            full (default) | metadata (no prompt/response text) | off
    ARCHFLOW_TRACE_MAX_CHARS   per-field cap on stored text (default 20000)
    OTEL_EXPORTER_OTLP_ENDPOINT  export Pydantic AI spans over OTLP (Langfuse, Phoenix, Jaeger, Logfire, ...);
                                 needs ``uv sync --extra otel``
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from .. import llm

log = logging.getLogger("archflow.traces")


def mode() -> str:
    m = os.environ.get("ARCHFLOW_TRACES", "full").strip().lower()
    return m if m in ("full", "metadata", "off") else "full"


def _max_chars() -> int:
    try:
        return int(os.environ.get("ARCHFLOW_TRACE_MAX_CHARS", "20000"))
    except ValueError:
        return 20000


def clip(value: Any) -> Any:
    """JSON-safe value with long text clipped; None in metadata mode (no content is kept)."""
    if mode() != "full" or value is None:
        return None
    limit = _max_chars()
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"… [{len(value) - limit} more chars]"
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) <= limit:
        return json.loads(text) if not isinstance(value, str) else text
    return text[:limit] + f"… [{len(text) - limit} more chars]"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Span:
    kind: str                       # model | tool | api
    name: str                       # model name, or tool name
    started_at: str = field(default_factory=_now)
    latency_ms: int = 0
    status: str = "ok"              # ok | error
    input: Any = None
    output: Any = None
    error: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0                  # gateway calls behind a model span (retries with feedback count)


@dataclass
class Trace:
    id: str
    org_id: str
    project_id: str
    feature: str
    prompt: str                     # id@version
    agent: str
    parent_id: str = ""             # the run that fanned this one out (multi-agent runs)
    user_id: str = ""               # who asked
    user_name: str = ""
    question: str = ""              # what they asked (short preview; the full prompt is in input)
    answer: str = ""                # what the AI answered (short preview; the full output is in output)
    started_at: str = field(default_factory=_now)
    status: str = "running"         # running | ok | error
    input: Any = None
    output: Any = None
    error: str = ""
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    models: list[str] = field(default_factory=list)
    spans: list[Span] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    _t0: float = field(default_factory=time.monotonic, repr=False)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("_t0", None)
        d["tool_calls"] = sum(1 for s in self.spans if s.kind == "tool")
        d["model_calls"] = sum(s.calls or 1 for s in self.spans if s.kind == "model")
        return d


TraceSink = Callable[[dict[str, Any]], None]
_sink: TraceSink | None = None
_current: ContextVar[Trace | None] = ContextVar("archflow_trace", default=None)
_listeners: list[TraceSink] = []  # in-process listeners (eval runner, tests)


def set_sink(sink: TraceSink | None) -> None:
    global _sink
    _sink = sink


@contextmanager
def listen(listener: TraceSink) -> Iterator[list[dict[str, Any]]]:
    """Collect finished traces in this process (the eval runner uses it to total cost per case)."""
    seen: list[dict[str, Any]] = []

    def collect(t: dict[str, Any]) -> None:
        seen.append(t)
        listener(t)

    _listeners.append(collect)
    try:
        yield seen
    finally:
        _listeners.remove(collect)


def current() -> Trace | None:
    return _current.get()


@contextmanager
def trace_run(prompt_ref: str, agent: str, *, org_id: str | None, input: Any, question: str = "",
              metadata: dict[str, Any] | None = None) -> Iterator[Trace | None]:
    """Open a trace for one agent run; every gateway call inside it is attached as cost/tokens. A run opened inside
    another (agents fired in parallel by a parent run) records it as ``parent_id``; the parent's totals include
    its children's calls."""
    if mode() == "off":
        yield None
        return
    scope = llm.current_scope()
    parent = _current.get()
    tr = Trace(id="tr_" + uuid.uuid4().hex[:20], org_id=org_id or (parent.org_id if parent else ""),
               project_id=scope.get("project_id", ""), feature=scope.get("feature") or agent.split(".")[0],
               prompt=prompt_ref, agent=agent, parent_id=parent.id if parent else "",
               user_id=scope.get("user_id", ""), user_name=scope.get("user_name", ""),
               question=preview(question) if mode() == "full" else "", input=clip(input), metadata=metadata or {})
    token = _current.set(tr)

    def on_call(event: dict[str, Any]) -> None:
        span = next((s for s in reversed(tr.spans) if s.kind == "model" and s.status == "running"), None)
        if span is not None:
            span.model = event["model"]
            span.prompt_tokens += event["prompt_tokens"]
            span.completion_tokens += event["completion_tokens"]
            span.cost_usd = round(span.cost_usd + event["cost_usd"], 6)
            span.calls += 1
        tr.prompt_tokens += event["prompt_tokens"]
        tr.completion_tokens += event["completion_tokens"]
        tr.cost_usd = round(tr.cost_usd + event["cost_usd"], 6)
        if event["model"] and event["model"] not in tr.models:
            tr.models.append(event["model"])

    try:
        with llm.observe_calls(on_call):
            yield tr
        if tr.status == "running":
            tr.status = "ok"
    except BaseException as exc:
        tr.status = "error"
        tr.error = f"{type(exc).__name__}: {exc}"[:2000]
        raise
    finally:
        _current.reset(token)
        tr.latency_ms = int((time.monotonic() - tr._t0) * 1000)
        _emit(tr)


@contextmanager
def model_span(name: str, input: Any) -> Iterator[Span | None]:
    """One model request made by an agent (the gateway may retry or fall back inside it)."""
    tr = _current.get()
    if tr is None:
        yield None
        return
    span = Span(kind="model", name=name, status="running", input=clip(input))
    tr.spans.append(span)
    t0 = time.monotonic()
    try:
        yield span
        span.status = "ok"
    except BaseException as exc:
        span.status, span.error = "error", f"{type(exc).__name__}: {exc}"[:1000]
        raise
    finally:
        span.latency_ms = int((time.monotonic() - t0) * 1000)


@contextmanager
def api_span(name: str, input: Any) -> Iterator[Span | None]:
    """A call to an external API inside a run (e.g. the Jev decision API)."""
    tr = _current.get()
    if tr is None:
        yield None
        return
    span = Span(kind="api", name=name, status="running", input=clip(input))
    tr.spans.append(span)
    t0 = time.monotonic()
    try:
        yield span
        span.status = "ok"
    except BaseException as exc:
        span.status, span.error = "error", f"{type(exc).__name__}: {exc}"[:1000]
        raise
    finally:
        span.latency_ms = int((time.monotonic() - t0) * 1000)


def preview(value: Any, limit: int = 500) -> str:
    """A one-paragraph preview of a question or answer for trace lists."""
    if value is None:
        return ""
    if isinstance(value, dict):
        text_key = next((k for k in ("answer", "message", "explanation", "rationale", "text")
                         if isinstance(value.get(k), str) and value[k].strip()), None)
        list_key = next((k for k in ("suggestions", "facts", "findings", "files", "components", "architectures")
                         if isinstance(value.get(k), list)), None)
        if text_key:
            value = value[text_key]
        elif list_key:  # suggestions, facts, findings...: their titles, not raw JSON
            items = value[list_key]
            names = [str(i.get(f)) for i in items if isinstance(i, dict)
                     for f in ("label", "text", "title", "path", "name") if i.get(f)][:6]
            value = f"{len(items)} {list_key}" + (": " + "; ".join(names) if names else "")
        elif isinstance(value.get("summary"), str) and value["summary"].strip():
            value = value["summary"]
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def set_answer(tr: Trace | None, output: Any) -> None:
    """Record a run's output (full, clipped) and its preview."""
    if tr is not None:
        tr.output = clip(output)
        tr.answer = preview(output) if mode() == "full" else ""


def add_tool_span(name: str, args: Any, result: Any, status: str = "ok") -> None:
    tr = _current.get()
    if tr is not None:
        tr.spans.append(Span(kind="tool", name=name, input=clip(args), output=clip(result), status=status))


def add_docs_spans(calls: list[dict[str, Any]]) -> None:
    """The documentation lookups (``cloud_docs.references``) made before the run, as API spans."""
    tr = _current.get()
    if tr is None:
        return
    for c in calls:
        tr.spans.append(Span(kind="api", name=f"docs: {c.get('source', '')}", latency_ms=int(c.get("latency_ms") or 0),
                             status="error" if c.get("error") else "ok", error=str(c.get("error") or ""),
                             input=clip({"query": c.get("query", "")}),
                             output=clip({k: c[k] for k in ("results", "cached", "skipped", "titles", "urls")
                                          if k in c})))
    if calls:
        tr.metadata["docs"] = [c.get("source") for c in calls if c.get("results")]


def _emit(tr: Trace) -> None:
    data = tr.to_dict()
    log.debug("trace %s %s %s %sms $%s", tr.id, tr.prompt, tr.status, tr.latency_ms, tr.cost_usd)
    for listener in list(_listeners):
        try:
            listener(data)
        except Exception:  # noqa: BLE001
            log.exception("trace listener failed")
    if _sink is not None:
        try:
            _sink(data)
        except Exception:  # noqa: BLE001 - tracing must never break a feature
            log.exception("storing trace %s failed", tr.id)


# --------------------------------------------------------------------------- OpenTelemetry
_otel_ready = False


def setup_otel() -> bool:
    """Export Pydantic AI's spans (agent runs, model requests, tool calls) over OTLP when an endpoint is set."""
    global _otel_ready
    if _otel_ready or not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return _otel_ready
    try:
        from opentelemetry import trace as otel_trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from pydantic_ai import Agent
        from pydantic_ai.models.instrumented import InstrumentationSettings
    except ImportError:
        log.warning("OTEL_EXPORTER_OTLP_ENDPOINT is set but OpenTelemetry is not installed (uv sync --extra otel)")
        return False
    provider = TracerProvider(resource=Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", "archflow")}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    otel_trace.set_tracer_provider(provider)
    Agent.instrument_all(InstrumentationSettings(tracer_provider=provider,
                                                 include_content=mode() == "full"))
    _otel_ready = True
    log.info("exporting agent traces over OTLP")
    return True
