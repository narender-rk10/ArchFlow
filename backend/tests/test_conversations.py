"""Conversations (shared, streamed AI chat threads) and project memory. No network: the LLM is faked."""

import json

import pytest
from authhelp import member_client, new_client, signup

from archflow import ai, llm
from archflow import conversations as cv
from archflow.main import create_app
from archflow.store import Store


@pytest.fixture()
def app():
    return create_app(Store(":memory:"))


@pytest.fixture()
def client(app):
    with new_client(app) as c:
        signup(c)
        yield c


@pytest.fixture()
def ai_off(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda org_id=None: False)


@pytest.fixture()
def ai_on(monkeypatch):
    """A fake LLM: streams a fixed answer, extracts one fact the first time, summarizes on request."""
    calls = {"stream": [], "json": [], "tools": []}
    facts = [[{"text": "Deployment target is AWS.", "category": "constraint"}]]

    def stream_text(system, messages, **kw):
        calls["stream"].append({"system": system, "messages": messages, **kw})
        yield "Redis caches "
        yield "**order** lookups."

    def complete_json(system, messages, schema, **kw):
        calls["json"].append({"system": system, "messages": messages, **kw})
        return {"facts": facts.pop(0) if facts else []}

    def complete_tools(system, messages, tools, **kw):
        calls["tools"].append({"system": system, "messages": messages, **kw})
        return "- earlier: they discussed caching", []

    monkeypatch.setattr(llm, "available", lambda org_id=None: True)
    monkeypatch.setattr(llm, "stream_text", stream_text)
    monkeypatch.setattr(llm, "complete_json", complete_json)
    monkeypatch.setattr(llm, "complete_tools", complete_tools)
    return calls


def project(client) -> str:
    r = client.post("/api/projects", json={"name": "Shop", "template": "ai-customer-support"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def thread(client, pid, kind="assistant", **body) -> dict:
    r = client.post(f"/api/projects/{pid}/conversations", json={"kind": kind, **body})
    assert r.status_code == 201, r.text
    return r.json()


def sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def ask(client, pid, cid, question) -> list[tuple[str, dict]]:
    r = client.post(f"/api/projects/{pid}/conversations/{cid}/ask", json={"question": question})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    return sse(r.text)


# --------------------------------------------------------------------------- Ask: streamed, remembered
def test_ask_streams_persists_and_remembers(client, ai_on):
    pid = project(client)
    conv = thread(client, pid)
    events = ask(client, pid, conv["id"], "We deploy on AWS only. Why was Redis added?")
    assert [e for e, _ in events] == ["start", "delta", "delta", "done"]
    assert events[0][1]["user_message"]["content"].startswith("We deploy on AWS")
    done = events[-1][1]["message"]
    assert done["role"] == "assistant" and done["content"] == "Redis caches **order** lookups."

    got = client.get(f"/api/projects/{pid}/conversations/{conv['id']}").json()
    assert got["title"] == "We deploy on AWS only. Why was Redis added?"
    assert [m["role"] for m in got["messages"]] == ["user", "assistant"]
    # The fact extractor ran after the reply (fast model) and stored a fact.
    assert ai_on["json"][0]["group"] == "fast"
    memory = client.get(f"/api/projects/{pid}/memory").json()
    assert [(m["text"], m["source"], m["conversation_id"]) for m in memory] == [
        ("Deployment target is AWS.", "ai", conv["id"])]

    # The follow-up sees the earlier turn and the memory.
    ask(client, pid, conv["id"], "And what if it goes down?")
    second = ai_on["stream"][1]
    assert [m["role"] for m in second["messages"]] == ["user", "assistant", "user"]
    assert "Why was Redis added?" in second["messages"][0]["content"]
    assert second["messages"][-1]["content"] == "And what if it goes down?"
    assert "<project_memory>" in second["system"] and "Deployment target is AWS." in second["system"]
    assert "<project name=\"Shop\"" in second["system"]
    assert client.get(f"/api/projects/{pid}/conversations").json()[0]["message_count"] == 4


def test_ask_needs_an_llm(client, ai_off):
    pid = project(client)
    conv = thread(client, pid)
    r = client.post(f"/api/projects/{pid}/conversations/{conv['id']}/ask", json={"question": "Why?"})
    assert r.status_code == 503
    assert client.get(f"/api/projects/{pid}/conversations/{conv['id']}").json()["messages"] == []


def test_failed_answer_is_kept_and_can_be_regenerated(client, ai_on, monkeypatch):
    pid = project(client)
    conv = thread(client, pid)

    def broken(system, messages, **kw):
        raise llm.LLMUnavailable("All LLM providers failed: rate limited")
        yield  # pragma: no cover - makes this a generator

    monkeypatch.setattr(llm, "stream_text", broken)
    events = ask(client, pid, conv["id"], "Which components handle PII?")
    assert events[-1][1]["message"]["data"]["error"].startswith("All LLM providers failed")
    assert client.get(f"/api/projects/{pid}/memory").json() == []  # nothing learned from a failed turn

    monkeypatch.setattr(llm, "stream_text", lambda s, m, **kw: iter(["The Ticket DB."]))
    r = client.post(f"/api/projects/{pid}/conversations/{conv['id']}/regenerate")
    assert r.status_code == 200, r.text
    assert sse(r.text)[-1][1]["message"]["content"] == "The Ticket DB."
    msgs = client.get(f"/api/projects/{pid}/conversations/{conv['id']}").json()["messages"]
    assert [(m["role"], m["content"]) for m in msgs] == [("user", "Which components handle PII?"),
                                                         ("assistant", "The Ticket DB.")]


def test_unanswered_question_is_merged_into_the_next(client, ai_on):
    pid = project(client)
    conv = thread(client, pid)
    store = client.app.state.store
    store.add_message(conv["id"], "user", "ask", "First question?", {}, "Owner")  # e.g. the answer crashed
    ask(client, pid, conv["id"], "Second question?")
    msgs = ai_on["stream"][0]["messages"]
    assert msgs == [{"role": "user", "content": "First question?\n\nSecond question?"}]


# --------------------------------------------------------------------------- edits with conversation context
def test_command_sees_recent_conversation(client, ai_on, monkeypatch):
    pid = project(client)
    conv = thread(client, pid)
    ask(client, pid, conv["id"], "What does the Order Service talk to?")
    seen = {}

    def run_command(state, text, selection, org_id=None, context=""):
        seen.update(text=text, context=context)
        return ai.CommandResult(ops=[{"op": "add_node", "node": {"name": "Order Cache", "type": "cache"}}],
                                explanation="Added a cache in front of it.", engine="llm")

    monkeypatch.setattr(ai, "run_command", run_command)
    r = client.post(f"/api/projects/{pid}/conversations/{conv['id']}/command",
                    json={"text": "put a cache in front of it", "selection": []})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["message"]["mode"] == "command" and body["message"]["data"]["applied"] is True
    assert body["message"]["data"]["summary"] and body["message"]["content"] == "Added a cache in front of it."
    assert "<recent_conversation>" in seen["context"] and "What does the Order Service talk to?" in seen["context"]
    assert "<project_memory>" in seen["context"]  # learned from the first exchange
    assert "put a cache in front of it" not in seen["context"]  # the request itself is passed separately
    assert "order-cache" in client.get(f"/api/projects/{pid}").json()["state"]["nodes"]


def test_legacy_command_and_ask_get_project_memory(client, ai_on, monkeypatch):
    pid = project(client)
    assert client.post(f"/api/projects/{pid}/memory", json={"text": "Budget is under $500/month",
                                                            "category": "constraint"}).status_code == 201
    seen = {}

    def run_command(state, text, selection, org_id=None, context=""):
        seen["context"] = context
        return ai.CommandResult(explanation="Nothing to do.", engine="llm")

    monkeypatch.setattr(ai, "run_command", run_command)
    assert client.post(f"/api/projects/{pid}/ai/command", json={"text": "tidy up"}).status_code == 200
    assert "Budget is under $500/month" in seen["context"]

    def claude_ask(context, question, org_id=None):
        seen["ask"] = context
        return "ok"

    monkeypatch.setattr(ai, "claude_ask", claude_ask)
    assert client.post(f"/api/projects/{pid}/ai/ask", json={"question": "cost?"}).json() == {"answer": "ok"}
    assert "Budget is under $500/month" in seen["ask"]


# --------------------------------------------------------------------------- memory management
def test_summary_folds_old_turns_offline(ai_off):
    store = Store(":memory:")
    pid = store.create_project("P")["id"]
    conv = store.create_conversation(pid, "main", "assistant", "", "Owner")
    for i in range(1, 26):
        store.add_message(conv["id"], "user" if i % 2 else "assistant", "ask", f"message {i}", {}, "Owner")
    assert cv.maybe_summarize(store, conv["id"], None) is True
    conv = store.get_conversation(conv["id"])
    assert conv["summarized_seq"] == 25 - cv.RECENT_MESSAGES
    assert "User: message 1" in conv["summary"] and "message 13" in conv["summary"]
    assert "message 14" not in conv["summary"]
    summary, recent = cv.window(store, conv)
    assert [m["content"] for m in recent][0] == "message 14" and len(recent) == cv.RECENT_MESSAGES
    assert cv.maybe_summarize(store, conv["id"], None) is False  # nothing new to fold


def test_summary_uses_fast_model_and_keeps_old_summary(ai_on):
    store = Store(":memory:")
    pid = store.create_project("P")["id"]
    conv = store.create_conversation(pid, "main", "assistant", "", "Owner")
    store.update_conversation(conv["id"], {"summary": "- they chose Postgres"})
    for i in range(cv.SUMMARIZE_AFTER + 1):
        store.add_message(conv["id"], "user" if i % 2 == 0 else "assistant", "ask", f"turn {i}", {}, "Owner")
    assert cv.maybe_summarize(store, conv["id"], "org-x")
    call = ai_on["tools"][0]
    assert call["group"] == "fast" and call["org_id"] == "org-x"
    assert "- they chose Postgres" in call["messages"][0]["content"]
    assert store.get_conversation(conv["id"])["summary"] == "- earlier: they discussed caching"


def test_regenerating_a_summarized_turn_resets_the_summary():
    store = Store(":memory:")
    pid = store.create_project("P")["id"]
    cid = store.create_conversation(pid, "main", "assistant", "", "Owner")["id"]
    for i in range(5):
        store.add_message(cid, "user", "ask", f"q{i}", {}, "Owner")
    store.update_conversation(cid, {"summary": "old", "summarized_seq": 3})
    assert store.delete_messages_from(cid, 5) == 1
    assert store.get_conversation(cid)["summary"] == "old"
    store.delete_messages_from(cid, 2)
    conv = store.get_conversation(cid)
    assert (conv["summary"], conv["summarized_seq"], conv["message_count"]) == ("", 0, 1)


def test_memory_rest_and_permissions(app, client, ai_on):
    pid = project(client)
    r = client.post(f"/api/projects/{pid}/memory", json={"text": "HIPAA applies to patient data",
                                                         "category": "constraint", "pinned": True})
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    assert client.post(f"/api/projects/{pid}/memory", json={"text": "hipaa applies to patient data!"}).status_code == 409
    assert client.post(f"/api/projects/{pid}/memory", json={"text": "x", "category": "nonsense"}).status_code == 422
    r = client.patch(f"/api/projects/{pid}/memory/{mid}", json={"text": "HIPAA applies to all patient data",
                                                               "pinned": False})
    assert r.json()["text"] == "HIPAA applies to all patient data" and r.json()["pinned"] is False

    viewer = member_client(app, client, "viewer", "viewer@example.com")
    assert viewer.get(f"/api/projects/{pid}/memory").json()[0]["id"] == mid
    assert viewer.post(f"/api/projects/{pid}/memory", json={"text": "Use GCP"}).status_code == 403
    assert viewer.delete(f"/api/projects/{pid}/memory/{mid}").status_code == 403

    # Viewers can ask in their own thread, but not edit the canvas or use the design chat.
    mine = thread(viewer, pid)
    ask(viewer, pid, mine["id"], "Which components store patient data?")
    r = viewer.post(f"/api/projects/{pid}/conversations/{mine['id']}/command", json={"text": "add a WAF"})
    assert r.status_code == 403
    assert viewer.post(f"/api/projects/{pid}/conversations", json={"kind": "design"}).status_code == 403

    # Threads are shared, but only their creator (or an admin) may rename, clear or delete them.
    owners = thread(client, pid, title="Owner's thread")
    assert {c["id"] for c in viewer.get(f"/api/projects/{pid}/conversations").json()} == {mine["id"], owners["id"]}
    assert viewer.patch(f"/api/projects/{pid}/conversations/{owners['id']}", json={"title": "x"}).status_code == 403
    assert viewer.delete(f"/api/projects/{pid}/conversations/{owners['id']}").status_code == 403
    assert viewer.patch(f"/api/projects/{pid}/conversations/{mine['id']}", json={"title": "Mine"}).json()["title"] == "Mine"
    assert client.delete(f"/api/projects/{pid}/conversations/{mine['id']}/messages").status_code == 204  # admin
    assert client.get(f"/api/projects/{pid}/conversations/{mine['id']}").json()["messages"] == []
    assert client.delete(f"/api/projects/{pid}/conversations/{mine['id']}").status_code == 204
    assert client.get(f"/api/projects/{pid}/conversations/{mine['id']}").status_code == 404
    assert client.delete(f"/api/projects/{pid}/memory/{mid}").status_code == 204
    # What is left is the fact the (fake) model learned from the viewer's question.
    assert [m["source"] for m in client.get(f"/api/projects/{pid}/memory").json()] == ["ai"]


def test_conversations_are_project_scoped(client, ai_on):
    a, b = project(client), project(client)
    conv = thread(client, a)
    assert client.get(f"/api/projects/{b}/conversations/{conv['id']}").status_code == 404
    r = client.post(f"/api/projects/{b}/conversations/{conv['id']}/ask", json={"question": "hi"})
    assert r.status_code == 404


# --------------------------------------------------------------------------- design chat
def test_design_chat_is_stored_and_answers_become_memory(client, ai_off):
    pid = project(client)
    conv = thread(client, pid, kind="design")
    r = client.post(f"/api/projects/{pid}/chat", json={
        "conversation_id": conv["id"], "turns": [{"role": "user", "content": "Food delivery marketplace"}]})
    assert r.status_code == 200, r.text
    card = r.json()["card"]
    assert card["type"] == "question" and r.json()["engine"] == "offline"
    pick = next((o for o in card["options"] if o.get("recommended")), card["options"][0])
    r = client.post(f"/api/projects/{pid}/chat", json={"conversation_id": conv["id"], "turns": [
        {"role": "user", "content": pick["label"], "answer": {"option_ids": [pick["id"]]}}]})
    assert r.status_code == 200, r.text

    memory = client.get(f"/api/projects/{pid}/memory").json()
    question = card["question"].rstrip("?")
    assert [(m["text"], m["source"]) for m in memory] == [(f"{question}: {pick['label']}", "design-chat")]

    r = client.post(f"/api/projects/{pid}/chat", json={"conversation_id": conv["id"], "turns": [
        {"role": "user", "content": "skip, show me the options"}]})
    arch = r.json()["card"]
    assert arch["type"] == "architectures"
    r = client.post(f"/api/projects/{pid}/chat/apply", json={
        "conversation_id": conv["id"], "option": arch["options"][0], "requirements": arch["requirements"],
        "mode": "branch", "branch_name": "draft/marketplace"})
    assert r.status_code == 200, r.text
    assert r.json()["conversation"]["branch"] == "draft/marketplace"

    got = client.get(f"/api/projects/{pid}/conversations/{conv['id']}").json()
    assert got["title"] == "Food delivery marketplace"
    roles = [(m["role"], m["mode"]) for m in got["messages"]]
    assert roles[:2] == [("user", "design"), ("assistant", "design")]
    assert roles[-2:] == [("user", "note"), ("assistant", "note")]
    assert got["messages"][-1]["data"]["applied"]["branch"] == "draft/marketplace"
    assert got["messages"][1]["data"]["card"]["id"] == card["id"]


def test_design_chat_answer_replaces_earlier_answer():
    store = Store(":memory:")
    pid = store.create_project("P")["id"]
    card = {"question": "Which cloud?", "options": []}
    cv.learn_design_answer(store, pid, "cv-1", card, ["AWS"], "Owner")
    cv.learn_design_answer(store, pid, "cv-1", card, ["Google Cloud"], "Owner")
    assert [m["text"] for m in store.list_memory(pid)] == ["Which cloud: Google Cloud"]


def test_design_chat_import_and_legacy_mode(client, ai_off):
    pid = project(client)
    turns = [{"role": "user", "content": "Internal HR portal"},
             {"role": "assistant", "content": "Who uses it?", "card": {"type": "question", "id": "users",
                                                                       "question": "Who uses it?", "options": []}}]
    conv = thread(client, pid, kind="design", messages=turns)
    assert conv["title"] == "Internal HR portal" and conv["message_count"] == 2
    msgs = client.get(f"/api/projects/{pid}/conversations/{conv['id']}").json()["messages"]
    assert msgs[1]["data"]["card"]["id"] == "users"
    # Without a conversation id the endpoint stays stateless (older clients).
    r = client.post(f"/api/projects/{pid}/chat", json={"turns": [{"role": "user", "content": "Chat app"}]})
    assert r.status_code == 200 and "user_message" not in r.json()
    # An assistant thread is not a design chat, and the design chat needs a user turn.
    other = thread(client, pid)
    r = client.post(f"/api/projects/{pid}/chat", json={"conversation_id": other["id"],
                                                       "turns": [{"role": "user", "content": "x"}]})
    assert r.status_code == 409
    r = client.post(f"/api/projects/{pid}/chat", json={"conversation_id": conv["id"], "turns": []})
    assert r.status_code == 422


# --------------------------------------------------------------------------- helpers
def test_chat_messages_alternate_and_start_with_user():
    msgs = [{"role": "assistant", "mode": "note", "content": "hello", "data": {}},
            {"role": "user", "mode": "ask", "content": "a", "data": {}},
            {"role": "user", "mode": "command", "content": "add redis", "data": {}},
            {"role": "assistant", "mode": "command", "content": "Done.", "data": {"summary": ["Added Redis"]}}]
    assert cv.chat_messages(msgs) == [
        {"role": "user", "content": "a\n\n[Canvas edit request] add redis"},
        {"role": "assistant", "content": "Done.\nChanges made: Added Redis"}]


def test_memory_block_budget_keeps_pinned():
    store = Store(":memory:")
    pid = store.create_project("P")["id"]
    store.add_memory(pid, "Pinned rule", pinned=True, category="constraint")
    for i in range(40):
        store.add_memory(pid, f"fact number {i} " + "x" * 100)
    block = cv.memory_block(store, pid)
    assert block.startswith("<project_memory>\n- [constraint] Pinned rule")
    assert len(block) < cv.MEMORY_PROMPT_CHARS + 300
    assert "fact number 39" in block and "fact number 0 " not in block  # newest first


def test_duplicates_and_fact_cap(monkeypatch):
    store = Store(":memory:")
    pid = store.create_project("P")["id"]
    assert cv.add_fact(store, pid, "Deployment target is AWS (eu-west-1).")
    assert cv.add_fact(store, pid, "deployment target is AWS eu-west-1") is None
    assert cv.add_fact(store, pid, "   ") is None
    monkeypatch.setattr(cv, "MEMORY_MAX_ITEMS", 2)
    assert cv.add_fact(store, pid, "Team knows Go")
    assert cv.add_fact(store, pid, "Budget is small") is None


def test_learning_skips_short_messages_and_offline(monkeypatch):
    store = Store(":memory:")
    pid = store.create_project("P")["id"]
    monkeypatch.setattr(llm, "available", lambda org_id=None: False)
    assert cv.learn_from_exchange(store, pid, "cv-1", "We must keep all data in the EU", "Noted.", None) == []
    monkeypatch.setattr(llm, "available", lambda org_id=None: True)
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: pytest.fail("short messages are not mined"))
    assert cv.learn_from_exchange(store, pid, "cv-1", "ok thanks", "You're welcome.", None) == []
