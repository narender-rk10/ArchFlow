"""Test helpers: authenticated TestClients.

Every client sends ``Origin: http://localhost:5173`` (the default trusted dev origin) so unsafe requests pass
the CSRF check, and keeps its own cookie jar, so one client = one signed-in browser.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from archflow import users

ORIGIN = "http://localhost:5173"
BASE = "http://localhost:8000"
PASSWORD = "correct horse battery staple"


def new_client(app, base_url: str = BASE, **kw) -> TestClient:
    return TestClient(app, base_url=base_url, follow_redirects=False, headers={"Origin": ORIGIN}, **kw)


def signup(client: TestClient, email: str = "owner@example.com", name: str = "Owner", password: str = PASSWORD,
           invite: str | None = None) -> dict:
    r = client.post("/api/auth/signup", json={"email": email, "name": name, "password": password,
                                              **({"invite": invite} if invite else {})})
    assert r.status_code == 201, r.text
    return r.json()


def login(client: TestClient, email: str, password: str = PASSWORD) -> dict:
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def authed_client(app, email: str = "owner@example.com", name: str = "Owner", base_url: str = BASE,
                  invite: str | None = None) -> TestClient:
    """A client signed up (first signup = owner of every existing org, incl. org-default) and signed in."""
    client = new_client(app, base_url)
    signup(client, email, name, invite=invite)
    return client


def member_client(app, owner: TestClient, role: str, email: str, name: str | None = None,
                  org_id: str = "org-default") -> TestClient:
    """A new user who joined ``org_id`` with ``role`` through an invite created by ``owner``."""
    inv = owner.post(f"/api/orgs/{org_id}/invites", json={"role": role})
    assert inv.status_code == 201, inv.text
    return authed_client(app, email, name or email.split("@")[0], invite=inv.json()["token"])


def session_cookie(client: TestClient) -> str:
    return client.cookies.get(users.SESSION_COOKIE)


def ws_headers(client: TestClient) -> dict:
    """TestClient websockets go to ws://testserver, so pass the session cookie explicitly."""
    return {"cookie": f"{users.SESSION_COOKIE}={session_cookie(client)}"}
