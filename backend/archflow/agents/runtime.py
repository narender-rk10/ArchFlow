"""Pydantic AI agents on top of ArchFlow's LLM gateway.

Every AI feature is a Pydantic AI ``Agent`` built from a versioned YAML prompt (``agents.prompts``). The agent's
model is a ``FunctionModel`` that sends each request through ``llm.py``, so the gateway's provider chain
(Gemini → Claude → OpenAI), per-org keys, retries, circuit breakers, rate limits, budgets and usage accounting all
still apply. Pydantic AI supplies the agent loop: tool execution with results fed back to the model, typed
outputs, message history, retries, usage limits and (optionally) OpenTelemetry instrumentation.

How a model request is sent:
- structured output and no tools  → ``llm.complete_json`` with the output schema (JSON mode, validated, one
  repair retry) and the result is returned as the output tool call;
- tools (and/or text output)      → ``llm.complete_tools`` with the tools in OpenAI format;
- streamed text                   → ``llm.stream_text``.

Helpers:
    run_json(prompt_id, schema, ...)   -> dict          structured output (JSON Schema)
    run_text(prompt_id, ...)           -> str
    stream_text(prompt_id, ...)        -> Iterator[str]
    run_tools(prompt_id, tools, deps, ...) -> ToolRunResult   agent loop with tools
Each run is traced (``agents.tracing``) and attributed to ``id@version`` in AI usage.

``docs=DocsRequest(query, about)`` first looks the request up in the official cloud documentation
(``cloud_docs``) and hands the result to the prompt as the ``references`` variable ('' when nothing applies);
the lookups are recorded as API spans of the run.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import queue
import threading
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Iterator

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from pydantic_ai import Agent, StructuredDict, Tool, UsageLimits  # noqa: E402
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded  # noqa: E402
from pydantic_ai.messages import (ModelMessage, ModelRequest, ModelResponse, RetryPromptPart,  # noqa: E402
                                  SystemPromptPart, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel  # noqa: E402

from .. import cloud_docs, llm  # noqa: E402
from . import prompts, tracing  # noqa: E402
from .prompts import PromptVersion  # noqa: E402

log = logging.getLogger("archflow.agents")

__all__ = ["run_json", "run_text", "stream_text", "run_tools", "ToolRunResult", "UsageLimitExceeded",
           "history_from", "tool_from_schema"]


# --------------------------------------------------------------------------- message conversion
def _content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):  # multi-part user prompt: keep the text parts
        return "\n".join(v if isinstance(v, str) else str(v) for v in value)
    return json.dumps(value, default=str)


def to_openai(messages: list[ModelMessage], instructions: str | None) -> tuple[str, list[dict[str, Any]]]:
    """Pydantic AI messages → (system prompt, OpenAI-format chat messages) for the gateway."""
    system: list[str] = []
    out: list[dict[str, Any]] = []
    for m in messages:
        if isinstance(m, ModelRequest):
            for p in m.parts:
                if isinstance(p, SystemPromptPart):
                    system.append(p.content)
                elif isinstance(p, UserPromptPart):
                    out.append({"role": "user", "content": _content(p.content)})
                elif isinstance(p, ToolReturnPart):
                    out.append({"role": "tool", "tool_call_id": p.tool_call_id, "name": p.tool_name,
                                "content": p.model_response_str()})
                elif isinstance(p, RetryPromptPart):
                    if p.tool_name:
                        out.append({"role": "tool", "tool_call_id": p.tool_call_id, "name": p.tool_name,
                                    "content": p.model_response()})
                    else:
                        out.append({"role": "user", "content": p.model_response()})
        elif isinstance(m, ModelResponse):
            text = "".join(p.content for p in m.parts if isinstance(p, TextPart))
            calls = [{"id": p.tool_call_id, "type": "function",
                      "function": {"name": p.tool_name, "arguments": p.args_as_json_str()}}
                     for p in m.parts if isinstance(p, ToolCallPart)]
            msg: dict[str, Any] = {"role": "assistant", "content": text}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
    if instructions:
        system.insert(0, instructions)
    return "\n\n".join(s for s in system if s), out


def history_from(messages: list[dict[str, Any]]) -> list[ModelMessage]:
    """OpenAI-style [{role, content}] user/assistant turns → Pydantic AI message history."""
    out: list[ModelMessage] = []
    for m in messages:
        if m.get("role") == "user":
            out.append(ModelRequest(parts=[UserPromptPart(content=str(m.get("content") or ""))]))
        elif m.get("role") == "assistant":
            out.append(ModelResponse(parts=[TextPart(content=str(m.get("content") or ""))]))
    return out


def _openai_tools(info: AgentInfo, include_output: bool) -> list[dict[str, Any]]:
    defs = list(info.function_tools) + (list(info.output_tools) if include_output else [])
    return [{"type": "function", "function": {"name": t.name, "description": t.description or "",
                                              "parameters": t.parameters_json_schema}} for t in defs]


def _call_id() -> str:
    return "call_" + uuid.uuid4().hex[:12]


@dataclass
class _Settings:
    group: str
    org_id: str | None
    max_tokens: int
    capture: list[ModelMessage] | None = None  # the latest request + response, kept for runs that end early


def gateway_model(settings: _Settings) -> FunctionModel:
    """A Pydantic AI model whose requests go through ``llm.py``."""

    def request(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        resp = _request(messages, info)
        if settings.capture is not None:
            settings.capture[:] = [*messages, resp]
        return resp

    def _request(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        system, msgs = to_openai(messages, info.instructions)
        structured_only = bool(info.output_tools) and not info.function_tools
        kind = "complete_json" if structured_only else "complete_tools"
        with tracing.model_span(kind, {"system": system, "messages": msgs[-6:],
                                       "tools": [t.name for t in info.function_tools]}) as span:
            if structured_only:
                out_tool = info.output_tools[0]
                data = llm.complete_json(system, msgs, out_tool.parameters_json_schema, group=settings.group,
                                         org_id=settings.org_id, max_tokens=settings.max_tokens)
                parts: list[Any] = [ToolCallPart(tool_name=out_tool.name, args=data, tool_call_id=_call_id())]
                if span is not None:
                    span.output = tracing.clip(data)
                return ModelResponse(parts=parts, model_name="archflow-gateway")
            text, calls = llm.complete_tools(system, msgs, _openai_tools(info, include_output=True),
                                             group=settings.group, org_id=settings.org_id,
                                             max_tokens=settings.max_tokens)
            parts = [TextPart(content=text)] if text else []
            parts += [ToolCallPart(tool_name=c["name"], args=c["arguments"], tool_call_id=_call_id()) for c in calls]
            if not parts:
                parts = [TextPart(content="")]
            if span is not None:
                span.output = tracing.clip({"text": text, "tool_calls": calls})
            return ModelResponse(parts=parts, model_name="archflow-gateway")

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        system, msgs = to_openai(messages, info.instructions)
        with tracing.model_span("stream_text", {"system": system, "messages": msgs[-6:]}) as span:
            parts: list[str] = []
            for chunk in llm.stream_text(system, msgs, group=settings.group, org_id=settings.org_id,
                                         max_tokens=settings.max_tokens):
                parts.append(chunk)
                yield chunk
            if span is not None:
                span.output = tracing.clip("".join(parts))

    return FunctionModel(request, stream_function=stream, model_name="archflow-gateway")


# --------------------------------------------------------------------------- running agents
def _in_thread(fn: Callable[[], Any]) -> Any:
    """Run ``fn`` here, or in a helper thread when this thread already runs an event loop (asyncio.run cannot nest)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return fn()
    box: dict[str, Any] = {}
    ctx = contextvars.copy_context()

    def target() -> None:
        try:
            box["value"] = ctx.run(fn)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def _run(make_coro: Callable[[], Any]) -> Any:
    """Run one agent to completion on a private event loop (the gateway calls inside it are blocking)."""
    return _in_thread(lambda: asyncio.run(make_coro()))


def _resolve(prompt_id: str, version: str | None, group: str | None, max_tokens: int | None,
             org_id: str | None) -> tuple[PromptVersion, _Settings]:
    p = prompts.get(prompt_id, version)
    return p, _Settings(group=group or p.group, org_id=org_id,
                        max_tokens=int(max_tokens or p.max_tokens or 8000))


def _question(question: str | None, variables: dict[str, Any], user: str | None, prompt_text: str) -> str:
    """What the person asked, for the trace list: given explicitly, else the request or question variable."""
    if question is not None:
        return question
    for key in ("question", "request", "last_question"):
        if isinstance(variables.get(key), str) and variables[key].strip():
            return variables[key]
    return user if user is not None else ""


def _user(p: PromptVersion, user: str | None, variables: dict[str, Any]) -> str:
    return user if user is not None else p.render_user(**variables)


def _with_docs(variables: dict[str, Any] | None,
               docs: cloud_docs.DocsRequest | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The variables with ``references`` set, and the lookups made (recorded once the run's trace is open)."""
    block, calls = cloud_docs.references(docs)
    return {**(variables or {}), "references": block}, calls


def _tool_spans(messages: list[ModelMessage], output_tool: str | None = None) -> None:
    calls: dict[str, ToolCallPart] = {}
    for m in messages:
        for p in m.parts:
            if isinstance(p, ToolCallPart) and p.tool_name != output_tool:
                calls[p.tool_call_id] = p
            elif isinstance(p, (ToolReturnPart, RetryPromptPart)) and p.tool_call_id in calls:
                c = calls.pop(p.tool_call_id)
                if isinstance(p, ToolReturnPart):
                    tracing.add_tool_span(c.tool_name, c.args_as_dict(), p.model_response_str())
                else:
                    tracing.add_tool_span(c.tool_name, c.args_as_dict(), p.model_response(), status="error")


def _translate(exc: Exception) -> Exception:
    if isinstance(exc, UnexpectedModelBehavior):
        return llm.LLMInvalidOutput(f"The model did not return a usable answer: {exc}")
    return exc


def run_json(prompt_id: str, schema: dict[str, Any], *, variables: dict[str, Any] | None = None,
             user: str | None = None, history: list[dict[str, Any]] | None = None, org_id: str | None = None,
             version: str | None = None, group: str | None = None, max_tokens: int | None = None,
             system_suffix: str = "", question: str | None = None,
             docs: cloud_docs.DocsRequest | None = None) -> dict[str, Any]:
    """Structured output validated against ``schema``. Raises llm.LLMUnavailable / LLMRefused like the gateway."""
    variables, lookups = _with_docs(variables, docs)
    p, settings = _resolve(prompt_id, version, group, max_tokens, org_id)
    system = p.render_system(**variables) + system_suffix
    prompt_text = _user(p, user, variables)
    agent = Agent(gateway_model(settings), output_type=StructuredDict(schema, name="result"), instructions=system,
                  name=prompt_id, retries=1)
    with llm.usage_scope(prompt=p.ref), \
            tracing.trace_run(p.ref, prompt_id, org_id=org_id, input={"user": prompt_text},
                              question=_question(question, variables, user, prompt_text)) as tr:
        tracing.add_docs_spans(lookups)
        try:
            result = _run(lambda: agent.run(prompt_text, message_history=history_from(history or [])))
        except Exception as exc:  # noqa: BLE001 - re-raised as the gateway's error types
            raise _translate(exc) from exc
        tracing.set_answer(tr, result.output)
        return dict(result.output)


def run_text(prompt_id: str, *, variables: dict[str, Any] | None = None, user: str | None = None,
             history: list[dict[str, Any]] | None = None, org_id: str | None = None, version: str | None = None,
             group: str | None = None, max_tokens: int | None = None, system_suffix: str = "",
             question: str | None = None, docs: cloud_docs.DocsRequest | None = None) -> str:
    variables, lookups = _with_docs(variables, docs)
    p, settings = _resolve(prompt_id, version, group, max_tokens, org_id)
    system = p.render_system(**variables) + system_suffix
    prompt_text = _user(p, user, variables)
    agent = Agent(gateway_model(settings), output_type=str, instructions=system, name=prompt_id)
    with llm.usage_scope(prompt=p.ref), \
            tracing.trace_run(p.ref, prompt_id, org_id=org_id, input={"user": prompt_text},
                              question=_question(question, variables, user, prompt_text)) as tr:
        tracing.add_docs_spans(lookups)
        try:
            result = _run(lambda: agent.run(prompt_text, message_history=history_from(history or [])))
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc) from exc
        text = (result.output or "").strip()
        tracing.set_answer(tr, text)
        return text


_DONE = object()


def stream_text(prompt_id: str, *, variables: dict[str, Any] | None = None, user: str | None = None,
                history: list[dict[str, Any]] | None = None, org_id: str | None = None,
                version: str | None = None, group: str | None = None, max_tokens: int | None = None,
                question: str | None = None, docs: cloud_docs.DocsRequest | None = None) -> Iterator[str]:
    """Yield the answer as it is written. The agent (and any documentation lookup) runs in a helper thread."""
    p, settings = _resolve(prompt_id, version, group, max_tokens, org_id)
    q: queue.Queue[Any] = queue.Queue()
    parts: list[str] = []
    run: dict[str, Any] = {}

    def prepare() -> None:
        variables, run["lookups"] = _with_docs(variables_in, docs)
        run["variables"], run["prompt_text"] = variables, _user(p, user, variables)
        run["agent"] = Agent(gateway_model(settings), output_type=str, instructions=p.render_system(**variables),
                             name=prompt_id)

    variables_in = variables

    async def consume() -> None:
        agent, prompt_text = run["agent"], run["prompt_text"]
        async with agent.run_stream(prompt_text, message_history=history_from(history or [])) as res:
            async for delta in res.stream_text(delta=True, debounce_by=None):  # forward chunks as they arrive
                if delta:
                    parts.append(delta)
                    q.put(delta)

    def worker() -> None:
        try:
            prepare()
            with llm.usage_scope(prompt=p.ref), \
                    tracing.trace_run(p.ref, prompt_id, org_id=org_id, input={"user": run["prompt_text"]},
                                      question=_question(question, run["variables"], user, run["prompt_text"])) as tr:
                tracing.add_docs_spans(run["lookups"])
                asyncio.run(consume())
                tracing.set_answer(tr, "".join(parts))
        except BaseException as exc:  # noqa: BLE001 - handed to the consumer
            q.put(("__error__", _translate(exc) if isinstance(exc, Exception) else exc))
        finally:
            q.put(_DONE)

    ctx = contextvars.copy_context()
    threading.Thread(target=lambda: ctx.run(worker), daemon=True).start()
    while True:
        item = q.get()
        if item is _DONE:
            return
        if isinstance(item, tuple) and item and item[0] == "__error__":
            raise item[1]
        yield item


# --------------------------------------------------------------------------- several agents at once
def parallel(tasks: dict[str, Callable[[], Any]], *, name: str, org_id: str | None, question: str = "",
             max_workers: int = 6) -> dict[str, Any]:
    """Fire several agents (or API calls) at once under one parent trace and wait for all of them.

    Returns {task name: result, or the exception it raised}; one failing agent never cancels the others. Each
    task runs in its own copy of the caller's context, so usage attribution (project, feature, user) carries
    over and every agent's trace records the parent's id."""
    from concurrent.futures import ThreadPoolExecutor

    results: dict[str, Any] = {}
    with tracing.trace_run(f"{name}@parallel", name, org_id=org_id, input={"agents": list(tasks)},
                           question=question) as tr:
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(tasks) or 1))) as pool:
            futures = {k: pool.submit(contextvars.copy_context().run, fn) for k, fn in tasks.items()}
            for k, f in futures.items():
                try:
                    results[k] = f.result()
                except Exception as exc:  # noqa: BLE001 - reported per task
                    results[k] = exc
        tracing.set_answer(tr, {k: f"failed: {type(v).__name__}: {v}" if isinstance(v, Exception) else "ok"
                                for k, v in results.items()})
        if tr is not None and all(isinstance(v, Exception) for v in results.values()) and results:
            tr.status, tr.error = "error", "every agent failed"
    return results


# --------------------------------------------------------------------------- tool agents
@dataclass
class ToolRunResult:
    output: str
    limit_reached: bool
    messages: list[ModelMessage]


def tool_from_schema(name: str, description: str, json_schema: dict[str, Any],
                     handler: Callable[..., str]) -> Tool:
    """A sequential agent tool with an explicit JSON schema; ``handler(deps, **args)`` returns the tool result."""
    def run(ctx, **args: Any) -> str:  # noqa: ANN001 - RunContext
        return handler(ctx.deps, **args)

    return Tool.from_schema(run, name=name, description=description, json_schema=json_schema, takes_ctx=True,
                            sequential=True)


def run_tools(prompt_id: str, tools: list[Tool], deps: Any, *, variables: dict[str, Any] | None = None,
              user: str | None = None, history: list[dict[str, Any]] | None = None, org_id: str | None = None,
              version: str | None = None, group: str | None = None, max_tokens: int | None = None,
              request_limit: int = 4, question: str | None = None,
              docs: cloud_docs.DocsRequest | None = None) -> ToolRunResult:
    """Run the agent loop: the model calls tools, sees their results, and finishes with a text answer.
    Stops after ``request_limit`` model requests (``limit_reached``); tool side effects live in ``deps``."""
    variables, lookups = _with_docs(variables, docs)
    p, settings = _resolve(prompt_id, version, group, max_tokens, org_id)
    captured: list[ModelMessage] = []
    settings.capture = captured
    system = p.render_system(**variables)
    prompt_text = _user(p, user, variables)
    agent = Agent(gateway_model(settings), output_type=str, instructions=system, name=prompt_id,
                  deps_type=type(deps), tools=tools)
    with llm.usage_scope(prompt=p.ref), \
            tracing.trace_run(p.ref, prompt_id, org_id=org_id, input={"user": prompt_text},
                              question=_question(question, variables, user, prompt_text)) as tr:
        tracing.add_docs_spans(lookups)
        try:
            result = _run(lambda: agent.run(prompt_text, deps=deps, message_history=history_from(history or []),
                                            usage_limits=UsageLimits(request_limit=request_limit)))
            messages, output, limited = result.all_messages(), (result.output or "").strip(), False
        except UsageLimitExceeded:
            messages, output, limited = captured, "", True
        except Exception as exc:  # noqa: BLE001
            _tool_spans(captured)
            raise _translate(exc) from exc
        _tool_spans(messages)
        tracing.set_answer(tr, output)
        if tr is not None:
            tr.metadata["limit_reached"] = limited
        return ToolRunResult(output=output, limit_reached=limited, messages=messages)
