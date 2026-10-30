"""The canvas command agent (Pydantic AI tool loop): every tool call is applied to a working copy and its result
goes back to the model, which keeps going until it answers in text or hits the request limit."""

import os

os.environ.setdefault("ARCHFLOW_DB", ":memory:")

from archflow import ai, llm  # noqa: E402
from archflow.agents import tracing  # noqa: E402
from archflow.ops import apply_ops  # noqa: E402
from archflow.templates import build_template  # noqa: E402


def scripted(responses):
    """Fake llm.complete_tools returning one scripted (text, calls) per model request, recording the messages."""
    requests = []

    def fake(system, messages, tools, **kw):
        requests.append({"system": system, "messages": messages, "tools": [t["function"]["name"] for t in tools]})
        return responses.pop(0) if responses else ("", [])
    return fake, requests


def tool_results(request):
    return [m["content"] for m in request["messages"] if m["role"] == "tool"]


def test_command_continues_until_model_stops(monkeypatch):
    fake, requests = scripted([
        ("", [{"name": "add_component", "arguments": {"name": "WAF", "type": "security"}}]),          # partial plan
        ("", [{"name": "add_component", "arguments": {"name": "Audit Log", "type": "service"}},
              {"name": "connect_components", "arguments": {"source": "order-service", "target": "Audit Log"}}]),
        ("Added a WAF and an audit log service.", []),                                                 # done
    ])
    monkeypatch.setattr(llm, "complete_tools", fake)
    state = build_template("ai-customer-support")
    res = ai.llm_command(state, "add a WAF and an audit log", [])
    assert [o["op"] for o in res.ops] == ["add_node", "add_node", "add_edge"]
    assert res.explanation == "Added a WAF and an audit log service."
    assert "add_component" in requests[0]["tools"] and "Request: add a WAF and an audit log" in requests[0]["messages"][0]["content"]
    assert tool_results(requests[1]) == ["applied: added security 'WAF'"]
    # the second batch ran in order: the new component exists before it is connected
    assert all(r.startswith("applied") for r in tool_results(requests[2])[1:])
    new, _ = apply_ops(state, res.ops)  # the accumulated batch applies cleanly to the real state
    assert "audit-log" in new.nodes


def test_rejected_call_is_fed_back(monkeypatch):
    fake, requests = scripted([
        ("", [{"name": "connect_components", "arguments": {"source": "nope", "target": "order-service"}}]),
        ("", [{"name": "add_component", "arguments": {"name": "Cache", "type": "cache"}}]),
        ("Done.", []),
    ])
    monkeypatch.setattr(llm, "complete_tools", fake)
    res = ai.llm_command(build_template("ai-customer-support"), "add a cache", [])
    rejected = tool_results(requests[1])[0]
    assert rejected.startswith("rejected") and "not found" in rejected
    assert [o["op"] for o in res.ops] == ["add_node"] and res.explanation == "Done."


def test_round_limit(monkeypatch):
    always = [("", [{"name": "add_component", "arguments": {"name": f"S{i}"}}]) for i in range(10)]
    fake, requests = scripted(always)
    monkeypatch.setattr(llm, "complete_tools", fake)
    res = ai.llm_command(build_template("blank"), "add services", [])
    assert len(requests) == ai.MAX_ROUNDS and len(res.ops) == ai.MAX_ROUNDS
    assert res.explanation.startswith("Changed:")


def test_questions_and_trace(monkeypatch):
    fake, _ = scripted([
        ("", [{"name": "ask_clarifying_questions", "arguments": {"questions": ["Which cloud?", "Which cloud?"]}},
              {"name": "add_component", "arguments": {"name": "API", "type": "service"}}]),
        ("Added an API; which cloud should it run on?", []),
    ])
    monkeypatch.setattr(llm, "complete_tools", fake)
    with tracing.listen(lambda t: None) as traces:
        res = ai.llm_command(build_template("blank"), "start an API", [])
    assert res.questions == ["Which cloud?"] and len(res.ops) == 1
    (tr,) = traces
    assert tr["prompt"] == "canvas.command@v2" and tr["status"] == "ok" and tr["model_calls"] == 2
    tools = [s for s in tr["spans"] if s["kind"] == "tool"]
    assert [s["name"] for s in tools] == ["ask_clarifying_questions", "add_component"]
    assert tools[1]["input"] == {"name": "API", "type": "service"} and tools[1]["output"].startswith("applied")
    assert tr["output"] == "Added an API; which cloud should it run on?" and "Request: start an API" in tr["input"]["user"]
