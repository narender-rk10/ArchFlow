"""Design-chat review-and-repair: deterministic checks on proposed options, one LLM repair call per flawed option
(mocked here — tests never need the network), rule-based fallback, option counts, requirements breadth,
technology de-duplication, offline composer quality and draft branch names."""

import copy
import json
import os

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import foodrush  # noqa: E402
import pytest  # noqa: E402
from authhelp import authed_client  # noqa: E402

from archflow import chat, llm, review  # noqa: E402
from archflow.analysis import validate_architecture  # noqa: E402
from archflow.main import create_app  # noqa: E402
from archflow.models import ProjectState  # noqa: E402
from archflow.ops import apply_ops  # noqa: E402
from archflow.store import Store  # noqa: E402

REVIEWER = "You are ArchFlow's architecture reviewer"


def repaired_foodrush(opt: dict) -> dict:
    """What a good repair looks like: auth + return path for the WebSocket gateway, KMS and the domain essentials."""
    fixed = copy.deepcopy(opt)
    fixed["components"] += [
        {"key": "kms", "name": "Key Management", "type": "security", "technology": "AWS KMS", "layer": 1},
        {"key": "notify", "name": "Notification Service", "type": "service", "technology": "Amazon SNS", "layer": 2},
        {"key": "maps", "name": "Maps & Routing", "type": "external", "technology": "Amazon Location Service", "layer": 3},
        {"key": "payouts", "name": "Payouts Service", "type": "service", "technology": "Stripe Connect", "layer": 2},
        {"key": "obs", "name": "Observability", "type": "service", "technology": "Amazon CloudWatch / Datadog", "layer": 3},
    ]
    fixed["connections"] += [
        {"from": "ws-gateway", "to": "auth-service", "protocol": "token validation"},
        {"from": "kafka", "to": "ws-gateway", "protocol": "subscribe"},
        {"from": "order-service", "to": "kms", "protocol": "KMS API"},
        {"from": "kafka", "to": "notify", "protocol": "consume"},
        {"from": "delivery-service", "to": "maps", "protocol": "HTTPS"},
        {"from": "kafka", "to": "payouts", "protocol": "consume"},
        {"from": "order-service", "to": "obs", "protocol": "OTLP"},
    ]
    return fixed


def serverless_alternative() -> dict:
    return {"id": "serverless", "name": "Serverless with managed realtime", "summary": "Lambda + DynamoDB",
            "pros": ["Scales to zero"], "cons": ["Cold starts"], "cost": "low", "complexity": "medium",
            "components": [
                {"key": "app", "name": "Customer App", "type": "client", "technology": "React Native", "layer": 0},
                {"key": "gw", "name": "API Gateway", "type": "gateway", "technology": "Amazon API Gateway", "layer": 1},
                {"key": "auth", "name": "Auth", "type": "security", "technology": "Amazon Cognito", "layer": 1},
                {"key": "fn", "name": "Order Functions", "type": "service", "technology": "AWS Lambda", "layer": 2},
                {"key": "db", "name": "Orders Table", "type": "database", "technology": "Amazon DynamoDB", "layer": 4}],
            "connections": [{"from": "app", "to": "gw"}, {"from": "gw", "to": "auth"}, {"from": "gw", "to": "fn"},
                            {"from": "fn", "to": "db"}]}


def fake_llm(monkeypatch, proposals, repair=None, extras=(), add_reqs=(), fail_repair=False, requirements=None):
    calls: list[dict] = []

    def complete_json(system, messages, schema, **kw):
        user = messages[-1]["content"]
        calls.append({"system": system, "user": user, "max_tokens": kw.get("max_tokens")})
        if system.startswith(REVIEWER):
            if fail_repair:
                raise llm.LLMUnavailable("provider down")
            opt = json.loads(user.split("Option to repair:\n", 1)[1].split("\n\nProblems", 1)[0])
            out = {"option": repair(opt) if repair else opt,
                   "fixes": ["Connected the WebSocket gateway to auth", "Kafka now feeds the WebSocket gateway"]}
            if "additional_architectures" in user:
                out["additional_architectures"] = copy.deepcopy(list(extras))
            if "functional areas" in user:
                out["requirements"] = list(add_reqs)
            return out
        return {"message": "Here are options.", "action": "propose", "architectures": copy.deepcopy(proposals),
                "requirements": foodrush.requirements() if requirements is None else requirements}

    monkeypatch.setattr(llm, "available", lambda org_id=None: True)
    monkeypatch.setattr(llm, "complete_json", complete_json)
    return calls


def propose(idea=foodrush.IDEA):
    turns = [{"role": "user", "content": idea}, {"role": "user", "content": "skip, show me the options"}]
    out = chat.next_turn(turns, ProjectState())
    assert out["engine"] == "llm" and out["card"]["type"] == "architectures"
    return out["card"]


def test_repair_call_gets_the_concrete_issues_and_the_repaired_option_is_kept(monkeypatch):
    calls = fake_llm(monkeypatch, [foodrush.flawed_option()], repair=repaired_foodrush,
                     extras=[serverless_alternative()],
                     add_reqs=[{"title": "Restaurants manage menus", "kind": "functional",
                                "acceptance_criteria": ["Menu edits visible within 60 s"]}])
    card = propose()
    proposal = calls[0]
    assert "Domain checklist (Delivery marketplace" in proposal["user"]  # the checklist feeds generation too
    repairs = [c for c in calls if c["system"].startswith(REVIEWER)]
    assert len(repairs) == 1  # at most one repair call per option
    prompt = repairs[0]["user"]
    assert "'WebSocket / MQTT Gateway' has no path to an identity/auth component" in prompt
    assert "Realtime return path missing" in prompt and "key-management" in prompt
    assert "additional_architectures" in prompt and "Order cancellation rules" in prompt  # more options + areas

    best = card["options"][0]
    rev = best["review"]
    assert rev["engine"] == "llm+rules" and best["recommended"]
    before = {i["code"] for i in rev["issues_before"]}
    assert {"unauthenticated_entry", "realtime_return_path", "missing_key_management"} <= before
    assert not [i for i in rev["issues_after"] if i["severity"] == "error"]
    assert len(rev["issues_after"]) < len(rev["issues_before"])
    assert "Kafka now feeds the WebSocket gateway" in rev["fixes"]
    conns = {(c["from"], c["to"]) for c in best["connections"]}
    assert ("kafka", "ws-gateway") in conns and ("ws-gateway", "auth-service") in conns
    names = {c["name"] for c in best["components"]}
    assert {"Key Management", "Notification Service", "Maps & Routing", "Payouts Service", "Observability"} <= names
    # one decided technology per component, alternatives kept in the description
    comps = {c["key"]: c for c in best["components"]}
    assert comps["restaurant-service"]["technology"] == "Go on EKS"
    assert "Node.js" in comps["restaurant-service"]["description"]
    assert comps["auth-service"]["technology"] == "Amazon Cognito"
    assert comps["obs"]["technology"] == "Amazon CloudWatch"
    assert not any(" / " in c["technology"] for o in card["options"] for c in o["components"])
    # 1 proposed option → the review asked for more; we end with the preferred 3 (LLM extra + composer fallback)
    assert len(card["options"]) == 3 and card["options"][1]["name"] == "Serverless with managed realtime"
    assert len({o["id"] for o in card["options"]}) == 3
    assert [o["recommended"] for o in card["options"]] == [True, False, False]
    # requirements breadth: the model's addition plus checklist stubs for what is still uncovered
    titles = {r["title"] for r in card["requirements"]}
    assert "Restaurants manage menus" in titles and "Partners manage their menu and availability" not in titles
    assert {"Refunds", "Payouts to partners", "Ratings and reviews", "Order cancellation rules"} <= titles
    assert len(card["requirements"]) >= 12


def test_worse_repair_is_discarded_and_rules_fix_the_original(monkeypatch):
    def destructive(opt):
        bad = copy.deepcopy(opt)
        bad["components"] = bad["components"][:3]
        bad["connections"] = []
        return bad

    second = foodrush.flawed_option()
    second.update(id="arch-2", name="Microservices with a managed broker")
    third = serverless_alternative()
    fake_llm(monkeypatch, [foodrush.flawed_option(), second, third], repair=destructive)
    card = propose()
    best = card["options"][0]
    assert best["review"]["engine"] == "rules"
    assert len(best["components"]) >= 15  # nothing dropped
    assert any("not better" in f for f in best["review"]["fixes"])
    # the rules still close the flagged flows
    conns = {(c["from"], c["to"]) for c in best["connections"]}
    assert ("ws-gateway", "auth-service") in conns and ("kafka", "ws-gateway") in conns
    assert not [i for i in best["review"]["issues_after"] if i["severity"] == "error"]


def test_repair_failure_degrades_to_rules_and_composer(monkeypatch):
    calls = fake_llm(monkeypatch, [foodrush.flawed_option()], fail_repair=True)
    card = propose()
    assert len([c for c in calls if c["system"].startswith(REVIEWER)]) == 1
    assert len(card["options"]) == 3
    assert all("review" in o for o in card["options"])
    assert not [i for o in card["options"] for i in o["review"]["issues_after"] if i["severity"] == "error"]
    assert card["options"][0]["recommended"] and not any(o["recommended"] for o in card["options"][1:])
    # the composer fallback runs on the cloud the model chose
    techs = " ".join(c["technology"] for c in card["options"][-1]["components"])
    assert "Amazon" in techs or "AWS" in techs
    assert any("built-in composer" in f for f in card["options"][-1]["review"]["fixes"])


def test_option_count_is_capped_and_clean_options_skip_the_repair_call(monkeypatch):
    many = []
    for i in range(6):
        o = serverless_alternative()
        o.update(id=f"o{i}", name=f"Option {i}")
        many.append(o)
    reqs = [{"title": "Track plants", "kind": "functional", "actors": ["Staff"], "acceptance_criteria": ["Listed in 1 s"]}]
    calls = fake_llm(monkeypatch, many, requirements=reqs)
    card = propose("Internal tool to track office plants")
    assert len(card["options"]) == 4
    assert not [c for c in calls if c["system"].startswith(REVIEWER)]  # no issues, no domain gaps: no repair calls
    assert all(o["review"]["issues_before"] == [] for o in card["options"])


@pytest.mark.parametrize("tech,expected,alts", [
    ("Go / Node.js on EKS", "Go on EKS", ["Node.js"]),
    ("Amazon Cognito / Keycloak", "Amazon Cognito", ["Keycloak"]),
    ("React Native / React", "React Native", ["React"]),
    ("PostgreSQL or MySQL", "PostgreSQL", ["MySQL"]),
    ("Redis (or Memcached)", "Redis", ["Memcached"]),
    ("TCP/IP gateway", "TCP/IP gateway", []),
    ("Amazon RDS PostgreSQL + PostGIS", "Amazon RDS PostgreSQL + PostGIS", []),
])
def test_one_technology_per_component(tech, expected, alts):
    assert review.normalize_technology(tech) == (expected, alts)


def _answers(**picks):
    return {k: {"option_ids": v, "labels": v} for k, v in picks.items()}


@pytest.mark.parametrize("idea,answers", [
    ("Food delivery marketplace with live tracking",
     _answers(users=["customers"], channels=["web", "mobile"], scale=["l"], cloud=["aws"], data=["transactional", "files"],
              priority=["scale"])),
    ("Internal knowledge assistant over company documents",
     _answers(channels=["web", "messaging"], scale=["m"], ai=["assistant", "agents"], data=["documents", "analytics"],
              priority=["compliance"])),
    ("Internal HR portal", _answers(scale=["s"], cloud=["azure"])),
])
def test_offline_composer_options_pass_the_flow_checks(idea, answers):
    card = chat.compose_architectures(idea, answers)
    assert 2 <= len(card["options"]) <= 4
    for o in card["options"]:
        st, _ = apply_ops(ProjectState(), chat.architecture_ops(ProjectState(), o, card["requirements"]))
        blocking = [i for i in validate_architecture(st, chat._context(idea, answers))
                    if i["severity"] in ("error", "warning")]
        assert blocking == [], (idea, o["name"], blocking)
        assert not any(" / " in c["technology"] for c in o["components"])


def test_offline_composer_adds_domain_capabilities_and_requirements():
    card = chat.compose_architectures("Food delivery marketplace: couriers deliver restaurant orders with live tracking",
                                      _answers(cloud=["gcp"]))
    for o in card["options"]:
        names = " ".join(c["name"] for c in o["components"])
        assert "Maps" in names and "Payouts" in names and "Observability" in names and "Realtime Gateway" in names
        assert o["review"]["fixes"]
    titles = {r["title"] for r in card["requirements"]}
    assert {"Refunds", "Payouts to partners", "Order and delivery notifications"} <= titles


def test_branch_names_cut_at_word_boundary_and_are_unique():
    client = authed_client(create_app(Store(":memory:")))
    pid = client.post("/api/projects", json={"name": "Demo"}).json()["id"]
    option = {**foodrush.flawed_option(), "name": "Managed Real-Time Microservices Architecture"}
    names = []
    for _ in range(2):
        r = client.post(f"/api/projects/{pid}/chat/apply", json={"option": option, "requirements": [], "mode": "branch"})
        assert r.status_code == 200, r.text
        names.append(r.json()["branch"])
    assert names == ["draft/managed-real-time-microservices", "draft/managed-real-time-microservices-2"]
    # an explicit name that exists is still a conflict
    r = client.post(f"/api/projects/{pid}/chat/apply",
                    json={"option": option, "requirements": [], "mode": "branch", "branch_name": names[0]})
    assert r.status_code == 409
