import os

os.environ.setdefault("ARCHFLOW_DB", ":memory:")

from archflow import ai, llm  # noqa: E402
from archflow.ops import apply_ops  # noqa: E402
from archflow.templates import build_template  # noqa: E402


def scripted(responses):
    """Fake llm.complete_tools returning one scripted (text, calls) per round, recording prompts."""
    prompts = []

    def fake(system, messages, tools, **kw):
        prompts.append(messages[-1]["content"])
        return responses.pop(0) if responses else ("", [])
    return fake, prompts


def test_command_continues_until_model_stops(monkeypatch):
    fake, prompts = scripted([
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
    assert "Changes already applied" in prompts[1] and "added security 'WAF'" in prompts[1]
    new, _ = apply_ops(state, res.ops)  # the accumulated batch applies cleanly to the real state
    assert "audit-log" in new.nodes


def test_rejected_batch_is_fed_back(monkeypatch):
    fake, prompts = scripted([
        ("", [{"name": "connect_components", "arguments": {"source": "nope", "target": "order-service"}}]),
        ("", [{"name": "add_component", "arguments": {"name": "Cache", "type": "cache"}}]),
        ("Done.", []),
    ])
    monkeypatch.setattr(llm, "complete_tools", fake)
    res = ai.llm_command(build_template("ai-customer-support"), "add a cache", [])
    assert "rejected" in prompts[1] and "not found" in prompts[1]
    assert [o["op"] for o in res.ops] == ["add_node"]


def test_round_limit(monkeypatch):
    always = [("", [{"name": "add_component", "arguments": {"name": f"S{i}"}}]) for i in range(10)]
    fake, prompts = scripted(always)
    monkeypatch.setattr(llm, "complete_tools", fake)
    res = ai.llm_command(build_template("blank"), "add services", [])
    assert len(prompts) == ai.MAX_ROUNDS and len(res.ops) == ai.MAX_ROUNDS
    assert res.explanation.startswith("Changed:")
