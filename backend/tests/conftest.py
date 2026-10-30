import os

# Tests never talk to a running collab server unless a test opts in by patching collab_sync.push_patch.
os.environ["ARCHFLOW_COLLAB_URL"] = ""
os.environ.setdefault("ARCHFLOW_INTERNAL_TOKEN", "test-internal-token")  # test-only value
os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ.pop("ARCHFLOW_SECRET_KEY", None)  # tests that need it set a test-only value via monkeypatch
os.environ["JEV_API_KEY"] = ""  # never call the real Jev API (set before .env is loaded; tests mock its transport)
os.environ["ARCHFLOW_DOCS"] = "off"  # never call the cloud documentation servers (test_cloud_docs fakes them)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_login_limiter():
    from archflow import users

    users.login_limiter.reset()
    yield
    users.login_limiter.reset()
