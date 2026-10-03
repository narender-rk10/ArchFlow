import os

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

from fastapi.testclient import TestClient  # noqa: E402

from archflow import catalog  # noqa: E402
from archflow.main import create_app  # noqa: E402
from archflow.models import NODE_TYPES  # noqa: E402
from archflow.store import Store  # noqa: E402
from archflow.templates import build_template  # noqa: E402


def test_catalog_is_consistent():
    ids = [c["id"] for c in catalog.CATALOG]
    assert len(ids) == len(set(ids))
    assert all(c["type"] in NODE_TYPES for c in catalog.CATALOG)
    # every cloud kind that AWS offers has an Azure and GCP equivalent, except genuine gaps
    no_native = {("ops.email", "gcp")}  # Google Cloud has no first-party email service
    for c in catalog.CATALOG:
        if c["vendor"] == "aws":
            for target in ("azure", "gcp"):
                assert catalog.equivalent(c, target) or (c["kind"], target) in no_native, (c["id"], target)


def test_match_prefers_distinctive_and_exact_names():
    m = lambda t: (catalog.match({"technology": t, "name": ""}) or {}).get("id")  # noqa: E731
    assert m("Kong") == "oss-kong"                       # not "Amazon API Gateway"
    assert m("PostgreSQL 16") == "oss-postgresql"       # not a managed variant
    assert m("Cloud SQL PostgreSQL") == "gcp-cloud-sql"
    assert m("Redis") == "oss-redis" and m("Memorystore Redis") == "gcp-memorystore"
    assert m("FastAPI") is None                          # app frameworks are not hosting platforms
    assert catalog.match({"catalog_id": "aws-lambda", "technology": "anything"})["id"] == "aws-lambda"


def test_convert_round_trip_via_api():
    client = TestClient(create_app(Store(":memory:")))
    pid = client.post("/api/projects", json={"name": "P", "template": "ai-customer-support"}).json()["id"]
    r = client.post(f"/api/projects/{pid}/convert", json={"target": "aws", "actor": "nk"}).json()
    assert r["changed"] == 3 and r["skipped"] == []
    nodes = client.get(f"/api/projects/{pid}").json()["state"]["nodes"]
    assert nodes["orders-db"]["technology"] == "Amazon RDS for PostgreSQL" and nodes["orders-db"]["vendor"] == "aws"
    assert nodes["orders-db"]["name"] == "Orders DB"     # custom names are kept
    client.post(f"/api/projects/{pid}/convert", json={"target": "azure"})
    nodes = client.get(f"/api/projects/{pid}").json()["state"]["nodes"]
    assert nodes["orders-db"]["catalog_id"] == "azure-postgres"
    assert client.post(f"/api/projects/{pid}/convert", json={"target": "ibm"}).status_code == 422
    assert len(client.get("/api/catalog").json()["items"]) == len(catalog.CATALOG)


def test_convert_keeps_third_party_saas():
    state = build_template("blank")
    from archflow.ops import apply_ops
    state, _ = apply_ops(state, [{"op": "add_node", "node": {"name": "Payments", "type": "external", "technology": "Stripe"}},
                                 {"op": "add_node", "node": {"name": "Cache", "type": "cache", "technology": "Upstash Redis"}}])
    ops, skipped = catalog.convert_ops(state, "gcp")
    assert [o["id"] for o in ops] == ["cache"] and not skipped
