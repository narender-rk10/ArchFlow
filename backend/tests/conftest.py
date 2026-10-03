import os

# Tests never talk to a running collab server unless a test opts in by patching collab_sync.push_patch.
os.environ["ARCHFLOW_COLLAB_URL"] = ""
os.environ.setdefault("ARCHFLOW_INTERNAL_TOKEN", "test-internal-token")  # test-only value
os.environ.setdefault("ARCHFLOW_DB", ":memory:")
