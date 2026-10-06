"""Where code comes from: public Git URLs (shallow clone) and, when allowed, server-local directories.

Security:
* Only ``https://`` URLs without credentials, on the default port or 443. The host is resolved and EVERY address
  must be public (no loopback, private, link-local, CGNAT, multicast, reserved or unspecified addresses), which
  blocks SSRF to the cloud metadata service and internal hosts. Git is then pinned to the vetted address with
  ``http.curloptResolve`` (no DNS-rebinding window) and redirects are disabled.
* ``git clone --depth 1 --single-branch --no-tags``, no submodules, no hooks, no LFS smudge, no symlinks, no
  credential prompts, only the https protocol; killed when it exceeds the time limit or the size cap.
* Local directories only with ``ARCHFLOW_ALLOW_LOCAL_REPOS=1`` (a self-hosted/dev setting: it lets members of
  any org read any directory the API process can read).
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit


class SourceError(ValueError):
    """The repository URL or path is not acceptable (HTTP 422)."""


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def clone_timeout() -> int:
    return _env_int("ARCHFLOW_CODEGRAPH_CLONE_TIMEOUT", 180)


def max_repo_bytes() -> int:
    return _env_int("ARCHFLOW_CODEGRAPH_MAX_REPO_MB", 500) * 1024 * 1024


def local_repos_allowed() -> bool:
    return os.environ.get("ARCHFLOW_ALLOW_LOCAL_REPOS", "").strip().lower() in ("1", "true", "yes")


_HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")


def is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return bool(addr.is_global) and not (addr.is_private or addr.is_loopback or addr.is_link_local
                                         or addr.is_multicast or addr.is_reserved or addr.is_unspecified)


def resolve_host(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SourceError(f"cannot resolve host '{host}'") from exc
    return sorted({info[4][0] for info in infos})


def validate_repo_url(url: str, resolver=resolve_host) -> tuple[str, str, str]:
    """Check a public Git URL. Returns (normalized url, host, a vetted public IP). Raises SourceError."""
    url = (url or "").strip()
    if len(url) > 500:
        raise SourceError("repository URL is too long")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise SourceError("only https:// Git URLs are supported")
    if parts.username or parts.password or "@" in parts.netloc:
        raise SourceError("credentials in the URL are not allowed; only public repositories can be analysed")
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError as exc:
        raise SourceError("invalid port") from exc
    if port not in (None, 443):
        raise SourceError("only the default https port is allowed")
    if not host or not _HOST_RE.match(host) or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise SourceError("repository host is not allowed")
    try:  # literal IPs must be public too
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
    addrs = [host] if literal else resolver(host)
    if not addrs:
        raise SourceError(f"cannot resolve host '{host}'")
    bad = [a for a in addrs if not is_public_ip(a)]
    if bad:
        raise SourceError("repository host resolves to a private, loopback or link-local address")
    path = parts.path or "/"
    if not re.fullmatch(r"/[A-Za-z0-9._~/-]+", path) or ".." in path:
        raise SourceError("unsupported repository path")
    return f"https://{host}{path.rstrip('/')}", host, addrs[0]


def repo_web_base(url: str | None) -> str | None:
    """The browsable base of a GitHub/GitLab repository URL (without .git), else None."""
    if not url:
        return None
    m = re.match(r"^https://(github\.com|gitlab\.com)/([^/]+(?:/[^/]+)+?)(?:\.git)?/?$", url)
    return f"https://{m.group(1)}/{m.group(2)}" if m else None


def _dir_size(path: Path, cap: int) -> int:
    total = 0
    for dirpath, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
            if total > cap:
                return total
    return total


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1", GIT_CONFIG_NOSYSTEM="1", GIT_ASKPASS="echo",
               GCM_INTERACTIVE="never")
    return env


def clone(url: str, dest: Path, *, resolver=resolve_host) -> str:
    """Shallow-clone a public repository into ``dest`` (must not exist). Returns the commit SHA."""
    norm, host, ip = validate_repo_url(url, resolver)
    if shutil.which("git") is None:
        raise RuntimeError("git is not installed on the server")
    pin = f"{host}:443:[{ip}]" if ":" in ip else f"{host}:443:{ip}"
    cmd = ["git", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "http.followRedirects=false",
           "-c", f"http.curloptResolve={pin}", "-c", "core.symlinks=false", "-c", "core.hooksPath=/dev/null",
           "-c", "credential.helper=", "-c", "submodule.recurse=false",
           "clone", "--depth", "1", "--single-branch", "--no-tags", "--quiet", norm, str(dest)]
    cap, deadline = max_repo_bytes(), time.monotonic() + clone_timeout()
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=_git_env(),
                            cwd=tempfile.gettempdir())
    try:
        while proc.poll() is None:
            if time.monotonic() > deadline:
                raise RuntimeError(f"clone timed out after {clone_timeout()} s")
            if dest.exists() and _dir_size(dest, cap) > cap:
                raise RuntimeError(f"repository is larger than {cap // (1024 * 1024)} MB")
            time.sleep(0.5)
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    err = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        raise RuntimeError(f"git clone failed: {err[-300:] or proc.returncode}")
    if _dir_size(dest, cap) > cap:
        raise RuntimeError(f"repository is larger than {cap // (1024 * 1024)} MB")
    sha = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], capture_output=True, text=True,
                         env=_git_env(), timeout=30)
    return sha.stdout.strip() if sha.returncode == 0 else ""


def check_local_path(path: str) -> Path:
    """A server-local directory, only when ARCHFLOW_ALLOW_LOCAL_REPOS=1."""
    if not local_repos_allowed():
        raise SourceError("analysing server-local directories is disabled (set ARCHFLOW_ALLOW_LOCAL_REPOS=1 on a "
                          "self-hosted server to enable it)")
    p = Path(path or "").expanduser()
    if not p.is_absolute():
        raise SourceError("local_path must be an absolute path")
    p = p.resolve()
    if not p.is_dir():
        raise SourceError("local_path is not a directory")
    return p


def local_commit(path: Path) -> str:
    """HEAD of a local Git checkout, or '' (never fails)."""
    if shutil.which("git") is None:
        return ""
    try:
        r = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True,
                           env=_git_env(), timeout=15)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""
