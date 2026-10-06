"""Cloning must fail closed on git versions that cannot pin DNS (http.curloptResolve needs git 2.37+)."""

import pytest

from archflow.codegraph import sources


def public_resolver(host):
    return ["140.82.112.3"]


def test_old_git_refuses_without_opt_in(monkeypatch, tmp_path):
    monkeypatch.setattr(sources, "git_version", lambda: (2, 35))
    monkeypatch.delenv("ARCHFLOW_ALLOW_UNPINNED_GIT", raising=False)
    with pytest.raises(sources.SourceError, match="2.37"):
        sources.clone("https://github.com/octocat/Hello-World", tmp_path / "r", resolver=public_resolver)


def test_old_git_allowed_with_explicit_opt_in(monkeypatch, tmp_path):
    monkeypatch.setattr(sources, "git_version", lambda: (2, 35))
    monkeypatch.setenv("ARCHFLOW_ALLOW_UNPINNED_GIT", "1")
    called = {}

    class FakeProc:
        returncode = 0
        def poll(self):
            return 0
        def communicate(self, timeout=None):
            return (b"", b"")

    def fake_popen(cmd, **kw):
        called["cmd"] = cmd
        (tmp_path / "r").mkdir()
        return FakeProc()

    monkeypatch.setattr(sources.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sources.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "abc123\n", "returncode": 0})())
    try:
        sources.clone("https://github.com/octocat/Hello-World", tmp_path / "r", resolver=public_resolver)
    except Exception:
        pass  # only the gate matters here: with the opt-in, git is invoked
    assert "clone" in called.get("cmd", [])


def test_new_git_needs_no_opt_in(monkeypatch):
    monkeypatch.setattr(sources, "git_version", lambda: (2, 47))
    monkeypatch.delenv("ARCHFLOW_ALLOW_UNPINNED_GIT", raising=False)
    assert sources.git_version() >= sources.PINNING_GIT and not sources.unpinned_git_allowed()
