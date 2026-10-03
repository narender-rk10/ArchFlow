"""End-to-end tests of the remote MCP server: OAuth 2.1 (DCR + PKCE + consent), MCP calls, scopes, isolation."""

import base64
import hashlib
import json
import os
import secrets
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from archflow.main import create_app  # noqa: E402
from archflow.store import Store  # noqa: E402

BASE = "http://localhost:8000"
REDIRECT = "http://localhost:9999/callback"
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
               "MCP-Protocol-Version": "2025-06-18"}


@pytest.fixture()
def env():
    store = Store(":memory:")
    with TestClient(create_app(store), base_url=BASE, follow_redirects=False) as client:
        yield client, store


def oauth_login(client, access="write", org_id="org-default", user="Claude Tester", scope="archflow:read archflow:write"):
    reg = client.post("/register", json={"redirect_uris": [REDIRECT], "client_name": "Test Agent",
                                         "token_endpoint_auth_method": "none",
                                         "grant_types": ["authorization_code", "refresh_token"], "scope": scope})
    assert reg.status_code == 201, reg.text
    client_id = reg.json()["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    r = client.get("/authorize", params={"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
                                         "code_challenge": challenge, "code_challenge_method": "S256",
                                         "state": "xyz", "scope": scope, "resource": f"{BASE}/mcp"})
    assert r.status_code == 302, r.text
    consent_url = urlparse(r.headers["location"])
    assert consent_url.path == "/oauth/consent"
    request_id = parse_qs(consent_url.query)["request"][0]
    page = client.get("/oauth/consent", params={"request": request_id})
    assert page.status_code == 200 and "Test Agent" in page.text
    r = client.post("/oauth/consent", data={"request": request_id, "decision": "approve", "org_id": org_id,
                                            "user_name": user, "access": access})
    assert r.status_code == 302, r.text
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["state"] == ["xyz"]
    tok = client.post("/token", data={"grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": REDIRECT,
                                      "client_id": client_id, "code_verifier": verifier, "resource": f"{BASE}/mcp"})
    assert tok.status_code == 200, tok.text
    # A code is single-use.
    again = client.post("/token", data={"grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": REDIRECT,
                                        "client_id": client_id, "code_verifier": verifier})
    assert again.status_code == 400
    return client_id, tok.json()


def mcp_call(client, token, method, params=None, id_=1):
    r = client.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
                    json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})
    return r


def tool(client, token, tool_name, **args):
    r = mcp_call(client, token, "tools/call", {"name": tool_name, "arguments": args})
    assert r.status_code == 200, r.text
    body = r.json()
    res = body["result"]
    text = res["content"][0]["text"] if res.get("content") else ""
    return res.get("isError", False), (res.get("structuredContent") or {}), text


def test_discovery_and_unauthenticated(env):
    client, _ = env
    prm = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"].rstrip("/") == f"{BASE}/mcp"
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["registration_endpoint"].endswith("/register") and "S256" in meta["code_challenge_methods_supported"]
    r = mcp_call(client, "nope", "tools/list")
    assert r.status_code == 401 and "resource_metadata" in r.headers.get("www-authenticate", "")


def test_oauth_flow_and_tools(env):
    client, store = env
    pid = client.post("/api/projects", json={"name": "Shop", "template": "ai-customer-support"}).json()["id"]
    _, tokens = oauth_login(client)
    at = tokens["access_token"]

    init = mcp_call(client, at, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                               "clientInfo": {"name": "pytest", "version": "1"}})
    assert init.status_code == 200 and init.json()["result"]["serverInfo"]["name"] == "archflow"
    names = {t["name"] for t in mcp_call(client, at, "tools/list").json()["result"]["tools"]}
    assert {"get_architecture", "add_component", "list_tasks", "get_task_brief", "report_progress"} <= names

    err, who, _ = tool(client, at, "whoami")
    assert not err and who["user"] == "Claude Tester" and who["access"] == "read-write"
    err, out, text = tool(client, at, "get_architecture", project_id=pid)
    assert not err and len(out["components"]) == 10

    err, out, text = tool(client, at, "add_component", project_id=pid, name="Payments", type="service", technology="Go")
    assert not err, text
    state = client.get(f"/api/projects/{pid}").json()["state"]
    assert "payments" in state["nodes"]
    assert client.get(f"/api/projects/{pid}/activity").json()[0]["actor"] == "Claude Tester via MCP"

    err, out, text = tool(client, at, "list_tasks", project_id=pid)
    tasks = json.loads(text) if not out else out.get("result", out)
    assert any(t["id"] == "REQ-003" for t in tasks)
    err, _, brief = tool(client, at, "get_task_brief", project_id=pid, requirement_id="REQ-003")
    assert "REQ-003" in brief and "Order Service" in brief
    err, out, text = tool(client, at, "report_progress", project_id=pid, requirement_id="REQ-003", status="implemented",
                          pr_url="https://github.com/acme/shop/pull/7")
    assert not err, text
    assert client.get(f"/api/projects/{pid}").json()["state"]["requirements"]["REQ-003"]["status"] == "implemented"
    assert "pull/7" in client.get(f"/api/projects/{pid}/activity").json()[0]["summary"][0]

    # Org isolation: a project in another org is invisible.
    other = client.post("/api/orgs", json={"name": "Other"}).json()["id"]
    foreign = client.post("/api/projects", json={"name": "Secret", "org_id": other}).json()["id"]
    err, _, text = tool(client, at, "get_architecture", project_id=foreign)
    assert err and "not found" in text
    err, out, text = tool(client, at, "list_projects")
    assert "Secret" not in text


def test_read_only_scope_and_refresh_rotation(env):
    client, _ = env
    pid = client.post("/api/projects", json={"name": "P", "template": "ai-customer-support"}).json()["id"]
    client_id, tokens = oauth_login(client, access="read")
    assert tokens["scope"] == "archflow:read"
    err, _, text = tool(client, tokens["access_token"], "add_component", project_id=pid, name="X")
    assert err and "read-only" in text
    err, _, _ = tool(client, tokens["access_token"], "get_architecture", project_id=pid)
    assert not err

    r = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["refresh_token"] != tokens["refresh_token"]
    reuse = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id})
    assert reuse.status_code == 400  # rotated refresh tokens cannot be replayed


def test_personal_access_token_lifecycle(env):
    client, store = env
    pid = client.post("/api/projects", json={"name": "P", "template": "blank"}).json()["id"]
    created = client.post("/api/orgs/org-default/tokens", json={"name": "CI", "user_name": "ci-bot", "access": "write"}).json()
    secret = created["token"]
    assert secret.startswith("afp_") and "token_hash" not in created
    err, _, text = tool(client, secret, "add_component", project_id=pid, name="Worker")
    assert not err, text
    listed = client.get("/api/orgs/org-default/tokens").json()
    assert listed[0]["name"] == "CI" and listed[0]["last_used_at"] and "token" not in listed[0]
    assert client.delete(f"/api/orgs/org-default/tokens/{created['id']}").status_code == 204
    assert mcp_call(client, secret, "tools/list").status_code == 401


def test_consent_denied(env):
    client, _ = env
    reg = client.post("/register", json={"redirect_uris": [REDIRECT], "client_name": "Nope", "token_endpoint_auth_method": "none"})
    cid = reg.json()["client_id"]
    r = client.get("/authorize", params={"response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
                                         "code_challenge": "x" * 43, "code_challenge_method": "S256", "state": "s"})
    rid = parse_qs(urlparse(r.headers["location"]).query)["request"][0]
    r = client.post("/oauth/consent", data={"request": rid, "decision": "deny"})
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["error"] == ["access_denied"] and q["state"] == ["s"]
