import os

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

from fastapi.testclient import TestClient  # noqa: E402

from archflow import chat  # noqa: E402
from archflow.main import create_app  # noqa: E402
from archflow.models import ProjectState  # noqa: E402
from archflow.ops import apply_ops  # noqa: E402
from archflow.store import Store  # noqa: E402

PICKS = {"users": ["customers"], "channels": ["web", "messaging"], "scale": ["l"], "cloud": ["aws"],
         "ai": ["assistant", "agents"], "data": ["transactional", "files"], "priority": ["scale"]}


def run_interview(turns, step):
    """Answer every question card with PICKS (or the recommended option) until architectures appear."""
    for _ in range(12):
        reply = step(turns)
        turns.append({"role": "assistant", "content": reply["message"], "card": reply["card"]})
        card = reply["card"]
        if card["type"] == "architectures":
            return card
        assert 2 <= len(card["options"]) <= 5
        ids = PICKS.get(card["id"]) or [next(o["id"] for o in card["options"] if o.get("recommended"))]
        turns.append({"role": "user", "content": "", "answer": {"option_ids": ids}})
    raise AssertionError("interview never proposed architectures")


def test_offline_interview_then_options():
    turns = [{"role": "user", "content": "Customer support platform with WhatsApp and web"}]
    card = run_interview(turns, lambda t: chat.next_turn(t, ProjectState()))
    assert 2 <= len(card["options"]) <= 4
    assert card["options"][0]["recommended"]
    styles = [o["style"] for o in card["options"]]
    assert styles[0] in ("microservices", "event_driven")  # large scale + scalability priority
    techs = {c["technology"] for o in card["options"] for c in o["components"]}
    assert any("Amazon" in t or "AWS" in t for t in techs)  # cloud answer maps to AWS services
    assert any(r["kind"] == "non_functional" for r in card["requirements"])


def test_skip_ahead_and_other_answer():
    turns = [{"role": "user", "content": "Internal HR portal"}]
    first = chat.next_turn(turns, ProjectState())
    turns.append({"role": "assistant", "content": "", "card": first["card"]})
    turns.append({"role": "user", "content": "", "answer": {"option_ids": [], "other": "Contractors"}})
    assert chat._answers(turns)[first["card"]["id"]]["labels"] == ["Contractors"]
    turns.append({"role": "user", "content": "skip, show me the options"})
    assert chat.next_turn(turns, ProjectState())["card"]["type"] == "architectures"


def test_architecture_ops_apply_cleanly_and_trace():
    turns = [{"role": "user", "content": "Order tracking app"}]
    card = run_interview(turns, lambda t: chat.next_turn(t, ProjectState()))
    for option in card["options"]:
        state, _ = apply_ops(ProjectState(), chat.architecture_ops(ProjectState(), option, card["requirements"]))
        assert len(state.nodes) == len(option["components"])
        assert state.edges and state.requirements
        assert any(n.requirement_ids for n in state.nodes.values())
        ys = {n.position.y for n in state.nodes.values()}
        assert len(ys) >= 3  # laid out in layers


def test_chat_api_apply_replace_and_branch():
    client = TestClient(create_app(Store(":memory:")))
    pid = client.post("/api/projects", json={"name": "Demo", "template": "ai-customer-support"}).json()["id"]
    turns = [{"role": "user", "content": "Customer support AI"}]
    card = run_interview(turns, lambda t: client.post(f"/api/projects/{pid}/chat", json={"turns": t}).json())
    opt = card["options"][1]

    r = client.post(f"/api/projects/{pid}/chat/apply",
                    json={"option": opt, "requirements": card["requirements"], "mode": "branch", "actor": "nk"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["branch"].startswith("draft/")
    draft = client.get(f"/api/projects/{pid}", params={"branch": out["branch"]}).json()
    assert {n["name"] for n in draft["state"]["nodes"].values()} == {c["name"] for c in opt["components"]}
    main = client.get(f"/api/projects/{pid}").json()
    assert "orders-db" in main["state"]["nodes"]  # main untouched

    r = client.post(f"/api/projects/{pid}/chat/apply",
                    json={"option": card["options"][0], "requirements": card["requirements"], "mode": "replace"})
    assert r.status_code == 200
    main = client.get(f"/api/projects/{pid}").json()
    assert "orders-db" not in main["state"]["nodes"]
    assert main["head_version"] == r.json()["version"]
    # original template requirements are kept, new ones appended without duplicate titles
    titles = [q["title"] for q in main["state"]["requirements"].values()]
    assert len(titles) == len(set(t.lower() for t in titles))
