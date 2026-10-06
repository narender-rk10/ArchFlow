"""Deterministic data-flow checks, domain completeness checklists and slug helpers."""

import os

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import foodrush  # noqa: E402

from archflow import chat, checklists  # noqa: E402
from archflow.analysis import full_analysis, validate_architecture  # noqa: E402
from archflow.models import ProjectState  # noqa: E402
from archflow.ops import apply_ops, slugify, unique_slug  # noqa: E402


def build(nodes, edges, reqs=()):
    """nodes: (id, type, name, tech); edges: (source, target[, protocol]); reqs: requirement dicts."""
    ops = [{"op": "add_requirement", "requirement": {"title": r.get("title", "R"), **r}} for r in reqs]
    ops += [{"op": "add_node", "node": {"id": i, "type": t, "name": n, "technology": tech}} for i, t, n, tech in nodes]
    ops += [{"op": "add_edge", "edge": {"source": e[0], "target": e[1], "protocol": e[2] if len(e) > 2 else ""}}
            for e in edges]
    state, _ = apply_ops(ProjectState(), ops)
    return state


def codes(state, context=""):
    return [i["code"] for i in validate_architecture(state, context)]


def issues_of(state, code):
    return [i for i in validate_architecture(state) if i["code"] == code]


BASE = [("web", "client", "Web App", "React"), ("gw", "gateway", "API Gateway", "Kong"),
        ("auth", "security", "Auth", "Keycloak"), ("svc", "service", "Orders", "FastAPI"),
        ("db", "database", "Orders DB", "PostgreSQL")]
BASE_EDGES = [("web", "gw"), ("gw", "auth"), ("gw", "svc"), ("svc", "db")]


def test_clean_baseline_has_no_flow_issues():
    assert codes(build(BASE, BASE_EDGES)) == []


# ---- entry points and authentication
def test_websocket_gateway_without_auth_is_an_unauthenticated_entry_point():
    nodes = BASE + [("ws", "gateway", "WebSocket Gateway", "Socket.IO")]
    state = build(nodes, BASE_EDGES + [("web", "ws", "WSS"), ("svc", "ws", "push")])
    found = issues_of(state, "unauthenticated_entry")
    assert [i["node_ids"] for i in found] == [["ws"]] and found[0]["severity"] == "error"
    # validating the token with the auth service fixes it
    assert "unauthenticated_entry" not in codes(build(nodes, BASE_EDGES + [("web", "ws"), ("svc", "ws"), ("ws", "auth")]))


def test_entry_point_authenticated_via_session_service_or_upstream_gateway():
    # web → ws → session service → auth (two hops through a synchronous service)
    nodes = BASE + [("ws", "gateway", "Realtime Gateway", "WebSocket"), ("sess", "service", "Session Service", "Go")]
    edges = BASE_EDGES + [("web", "ws"), ("ws", "sess"), ("sess", "auth"), ("svc", "ws")]
    assert "unauthenticated_entry" not in codes(build(nodes, edges))
    # load balancer in front of the authenticated API gateway, and an internal gateway behind it
    nodes = BASE + [("lb", "gateway", "Load Balancer", "AWS ALB"), ("igw", "gateway", "Internal Gateway", "Envoy")]
    edges = [("web", "lb"), ("lb", "gw"), ("gw", "auth"), ("gw", "igw"), ("igw", "svc"), ("svc", "db")]
    assert "unauthenticated_entry" not in codes(build(nodes, edges))


def test_cdn_is_not_an_unauthenticated_entry_point():
    nodes = BASE + [("cdn", "gateway", "CDN", "Amazon CloudFront"), ("media", "storage", "Media", "Amazon S3")]
    edges = BASE_EDGES + [("web", "cdn"), ("cdn", "media"), ("svc", "media", "upload")]
    assert "unauthenticated_entry" not in codes(build(nodes, edges))


def test_client_calling_a_service_directly_needs_auth():
    nodes = BASE + [("pub", "service", "Public API", "FastAPI")]
    state = build(nodes, BASE_EDGES + [("web", "pub"), ("pub", "db")])
    assert [i["node_ids"] for i in issues_of(state, "unauthenticated_entry")] == [["pub"]]


def test_no_identity_component_at_all_is_reported_once():
    state = build([("web", "client", "Web", "React"), ("gw", "gateway", "Gateway", "Kong"), ("svc", "service", "Svc", "Go")],
                  [("web", "gw"), ("gw", "svc")])
    found = issues_of(state, "unauthenticated_entry")
    assert len(found) == 1 and found[0]["severity"] == "warning"


# ---- queues
def test_queue_with_producers_but_no_consumers():
    nodes = BASE + [("q", "queue", "Jobs", "RabbitMQ")]
    state = build(nodes, BASE_EDGES + [("svc", "q", "enqueue")])
    assert "queue_no_consumer" in codes(state)
    # a worker drawn as "worker → queue (consume)" is a consumer
    nodes2 = nodes + [("w", "service", "Worker", "Go")]
    assert "queue_no_consumer" not in codes(build(nodes2, BASE_EDGES + [("svc", "q"), ("w", "q", "consume"), ("w", "db")]))


def test_queue_with_consumers_but_no_producers():
    nodes = BASE + [("q", "queue", "Events", "Kafka"), ("w", "service", "Worker", "Go")]
    state = build(nodes, BASE_EDGES + [("q", "w"), ("w", "db")])
    assert "queue_no_producer" in codes(state)


# ---- realtime return path
def test_realtime_channel_that_only_feeds_the_bus_has_no_return_path():
    nodes = BASE + [("ws", "gateway", "WebSocket Gateway", "Socket.IO"), ("bus", "queue", "Event Bus", "Kafka"),
                    ("track", "service", "Tracking", "Go")]
    edges = BASE_EDGES + [("web", "ws", "WSS"), ("ws", "auth"), ("ws", "bus", "publish"), ("bus", "track")]
    found = issues_of(build(nodes, edges), "realtime_return_path")
    assert len(found) == 1 and found[0]["severity"] == "error" and "Event Bus" in found[0]["message"]
    # the bus delivering back to the gateway, or the gateway subscribing, closes the loop
    assert "realtime_return_path" not in codes(build(nodes, edges + [("bus", "ws", "subscribe")]))
    edges_sub = BASE_EDGES + [("web", "ws"), ("ws", "auth"), ("ws", "bus", "publish/subscribe"), ("bus", "track")]
    assert "realtime_return_path" not in codes(build(nodes, edges_sub))


def test_realtime_channel_not_connected_to_backend():
    nodes = BASE + [("ws", "gateway", "MQTT Broker", "EMQX")]
    found = issues_of(build(nodes, BASE_EDGES + [("web", "ws"), ("ws", "auth")]), "realtime_return_path")
    assert len(found) == 1 and found[0]["severity"] == "warning"


def test_push_notification_service_is_not_a_realtime_channel():
    nodes = BASE + [("push", "service", "Push Notification Service", "Firebase Cloud Messaging")]
    assert "realtime_return_path" not in codes(build(nodes, BASE_EDGES + [("svc", "push"), ("push", "web")]))


# ---- data stores and reachability
def test_datastore_without_writer_and_without_reader():
    nodes = BASE + [("wh", "database", "Warehouse", "ClickHouse"), ("bi", "client", "BI", "Metabase")]
    state = build(nodes, BASE_EDGES + [("svc", "wh", "read")])
    assert "datastore_no_writer" in codes(state)
    state = build(BASE + [("arch", "storage", "Archive", "S3")], BASE_EDGES + [("svc", "arch", "write")])
    assert [i["severity"] for i in issues_of(state, "datastore_no_reader")] == ["info"]


def test_unreachable_service():
    nodes = BASE + [("lonely", "service", "Reporting", "Python")]
    state = build(nodes, BASE_EDGES + [("lonely", "db", "read")])
    assert [i["node_ids"] for i in issues_of(state, "unreachable")] == [["lonely"]]
    # scheduled jobs and queue consumers are reachable
    nodes = BASE + [("job", "service", "Nightly Report Job", "Python"), ("q", "queue", "Q", "SQS"), ("w", "service", "W", "Go")]
    edges = BASE_EDGES + [("job", "db", "read"), ("svc", "q"), ("w", "q", "consume"), ("w", "db")]
    assert "unreachable" not in codes(build(nodes, edges))


# ---- requirement-driven checks
GDPR = {"title": "GDPR", "kind": "non_functional", "category": "Compliance",
        "description": "PII is encrypted at rest; right to be forgotten within 30 days."}


def test_encryption_nfr_requires_key_management():
    assert "missing_key_management" in codes(build(BASE, BASE_EDGES, [GDPR]))
    nodes = BASE + [("kms", "security", "Key Management", "AWS KMS")]
    assert "missing_key_management" not in codes(build(nodes, BASE_EDGES + [("svc", "kms")], [GDPR]))
    # recognised through the catalog too (Azure Key Vault is a security.secrets kind)
    nodes = BASE + [("kv", "security", "Secrets", "Azure Key Vault")]
    assert "missing_key_management" not in codes(build(nodes, BASE_EDGES + [("svc", "kv")], [GDPR]))


def test_availability_nfr_flags_single_instance_databases():
    req = {"title": "Uptime", "kind": "non_functional", "description": "99.9% monthly uptime"}
    found = issues_of(build(BASE, BASE_EDGES, [req]), "single_instance_datastore")
    assert found and found[0]["severity"] == "info"
    nodes = BASE[:-1] + [("db", "database", "Orders DB", "PostgreSQL (multi-AZ)")]
    assert not issues_of(build(nodes, BASE_EDGES, [req]), "single_instance_datastore")


def test_scale_nfr_without_cache_or_queue():
    req = {"title": "Scale", "kind": "non_functional", "description": "Handles 50,000 orders per day with lunch peaks"}
    assert "no_buffer_for_scale" in codes(build(BASE, BASE_EDGES, [req]))
    small = {"title": "Scale", "kind": "non_functional", "description": "Up to 500 users; load test at 2x expected peak"}
    assert "no_buffer_for_scale" not in codes(build(BASE, BASE_EDGES, [small]))
    nodes = BASE + [("cache", "cache", "Cache", "Redis")]
    assert "no_buffer_for_scale" not in codes(build(nodes, BASE_EDGES + [("svc", "cache")], [req]))


def test_erasure_vs_immutable_event_log():
    nodes = BASE + [("bus", "queue", "Event Bus", "Apache Kafka"), ("w", "service", "W", "Go")]
    edges = BASE_EDGES + [("svc", "bus"), ("bus", "w")]
    assert "erasure_event_log" in codes(build(nodes, edges, [GDPR]))


# ---- the real FoodRush option
def foodrush_state(option=None):
    option = option or foodrush.flawed_option()
    state, _ = apply_ops(ProjectState(), chat.architecture_ops(ProjectState(), option, foodrush.requirements()))
    return state


def test_foodrush_gaps_found_by_ai_review_are_now_caught_deterministically():
    a = full_analysis(foodrush_state())
    found = {(i["code"], tuple(i["node_ids"])) for i in a["architecture_issues"]}
    assert ("unauthenticated_entry", ("ws-gateway",)) in found
    assert ("realtime_return_path", ("ws-gateway",)) in found
    assert any(c == "missing_key_management" for c, _ in found)
    assert any(c == "erasure_event_log" for c, _ in found)
    caps = {i.get("capability") for i in a["architecture_issues"] if i["code"] == "completeness"}
    assert {"notifications", "maps_geo", "payouts", "observability"} <= caps
    assert a["health"]["architecture_score"] < 60  # was 93 % before these checks


# ---- checklists
def test_domain_detection():
    assert checklists.detect_domains(foodrush.IDEA)[0] == "delivery"
    assert "rag" in checklists.detect_domains("Internal knowledge assistant over company documents")
    assert "fintech" in checklists.detect_domains("A mobile wallet with money transfer between friends")
    assert "iot" in checklists.detect_domains("Collect telemetry from 10k sensors in factories")
    assert checklists.detect_domains("Internal HR portal") == []


def test_capability_satisfaction_by_type_words_and_catalog_kind():
    assert checklists.satisfied("media_storage", [{"type": "storage", "name": "Files", "technology": ""}])
    assert checklists.satisfied("notifications", [{"type": "external", "name": "Messaging", "technology": "Twilio"}])
    assert checklists.satisfied("observability", [{"type": "service", "name": "Telemetry", "technology": "Datadog"}])
    assert checklists.satisfied("key_management", [{"type": "security", "name": "Secrets", "technology": "AWS Secrets Manager"}])
    assert not checklists.satisfied("payouts", [{"type": "external", "name": "Payments", "technology": "Stripe"}])
    assert checklists.satisfied("live_tracking", [], {"realtime_return": True})


def test_missing_requirement_areas_for_a_delivery_marketplace():
    missing = checklists.missing_areas(foodrush.IDEA, foodrush.requirements())
    assert {"menu_management", "cancellations", "refunds", "ratings", "payouts", "notifications"} <= set(missing)
    assert "live_tracking" not in missing and "assignment" not in missing
    stub = checklists.requirement_stub("refunds")
    assert stub["kind"] == "functional" and stub["acceptance_criteria"] and any(ch.isdigit() for ch in stub["acceptance_criteria"][0])


def test_every_checklist_reference_exists():
    for d in checklists.DOMAINS.values():
        assert set(d["capabilities"]) <= set(checklists.CAPABILITIES)
        assert set(d["areas"]) <= set(checklists.AREAS)
    for cap in checklists.CAPABILITIES.values():
        comp = cap.get("component")
        if comp:
            assert set(comp["tech"]) == {"aws", "azure", "gcp", "agnostic"} and comp["attach"]


# ---- slugs / branch names
def test_slugify_cuts_at_a_word_boundary():
    s = slugify("Managed Real-Time Microservices Architecture")
    assert s == "managed-real-time-microservices" and len(s) <= 40
    assert slugify("x" * 60) == "x" * 40
    assert not slugify("A " * 30).endswith("-")


def test_unique_slug_appends_counter_within_limit():
    taken = {"draft/managed-real-time-microservices"}
    name = "Managed Real-Time Microservices Architecture"
    assert unique_slug(name, taken, prefix="draft/") == "draft/managed-real-time-microservices-2"
    taken.add("draft/managed-real-time-microservices-2")
    third = unique_slug(name, taken, prefix="draft/")
    assert third == "draft/managed-real-time-microservices-3" and len(third) - len("draft/") <= 40
    long_taken = {"draft/" + slugify("alpha beta gamma delta epsilon zeta eta theta")}
    assert len(unique_slug("alpha beta gamma delta epsilon zeta eta theta", long_taken, prefix="draft/")) - 6 <= 40
