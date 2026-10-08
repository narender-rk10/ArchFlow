"""Encryption at rest for per-org secrets (LLM provider API keys).

AES-256-GCM with a 96-bit random nonce per value; the key is derived with HKDF-SHA256 from the
``ARCHFLOW_SECRET_KEY`` environment variable (any long random string, e.g. ``openssl rand -base64 32``).
The org id and provider name are bound to each ciphertext as associated data, so a value copied to another
row does not decrypt. Without ``ARCHFLOW_SECRET_KEY`` the feature is disabled (``SecretsUnavailable``).

Rotating ARCHFLOW_SECRET_KEY makes stored keys unreadable: admins then re-enter them (they are ignored, and
the env keys are used, until they do).

Uses the ``cryptography`` package, which is already installed as a dependency of the MCP SDK.
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
from pathlib import Path

from .ai_providers import PROVIDERS as _CATALOG

PROVIDERS = tuple(_CATALOG)
_VERSION = "v1"


class SecretsUnavailable(RuntimeError):
    """ARCHFLOW_SECRET_KEY is not set (or the cryptography package is missing)."""


class SecretDecryptError(ValueError):
    """The ciphertext is corrupt, bound to another row, or was encrypted under a different key."""


def _key_file() -> Path | None:
    """Where a generated key is kept when ARCHFLOW_SECRET_KEY is not set: next to the SQLite database (the API's
    data volume in Docker), or ARCHFLOW_SECRET_KEY_FILE. None for in-memory databases (tests)."""
    explicit = os.environ.get("ARCHFLOW_SECRET_KEY_FILE", "").strip()
    if explicit:
        return Path(explicit)
    db = os.environ.get("ARCHFLOW_DB", "")
    if db == ":memory:":
        return None
    base = Path(db).parent if db else Path(__file__).resolve().parent.parent / "data"
    return base / "secret.key"


_generated: str | None = None


def _master() -> str:
    """ARCHFLOW_SECRET_KEY, else a key generated once and kept in the data directory (so admins can enter AI keys
    in the UI without editing the environment). Back the key file up with the database: losing it makes stored
    keys unreadable (admins then re-enter them)."""
    global _generated
    env = os.environ.get("ARCHFLOW_SECRET_KEY", "").strip()
    if env:
        return env
    if _generated:
        return _generated
    path = _key_file()
    if path is None:
        return ""
    try:
        if path.is_file():
            _generated = path.read_text(encoding="utf-8").strip()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            value = secrets.token_urlsafe(48)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(value)
            _generated = value
            logging.getLogger("archflow.secrets").warning(
                "ARCHFLOW_SECRET_KEY is not set: generated %s to encrypt organizations' AI keys. Back it up with "
                "the database, or set ARCHFLOW_SECRET_KEY.", path)
    except OSError:
        logging.getLogger("archflow.secrets").exception("cannot create the secret key file %s", path)
        return ""
    return _generated or ""


def enabled() -> bool:
    return len(_master()) >= 16


def _key() -> bytes:
    secret = _master()
    if len(secret) < 16:
        raise SecretsUnavailable(
            "Storing AI provider keys requires ARCHFLOW_SECRET_KEY (at least 16 characters, e.g. the output of "
            "`openssl rand -base64 32`) in the API's environment. Set it and restart the API.")
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError as exc:  # pragma: no cover - installed with the MCP SDK
        raise SecretsUnavailable("The 'cryptography' package is not installed") from exc
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"archflow-secrets",
                info=b"archflow org secrets v1").derive(secret.encode())


def encrypt(plaintext: str, aad: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(12)
    ct = AESGCM(_key()).encrypt(nonce, plaintext.encode(), aad.encode())
    return f"{_VERSION}:{base64.b64encode(nonce + ct).decode()}"


def decrypt(token: str, aad: str) -> str:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        version, b64 = token.split(":", 1)
        if version != _VERSION:
            raise SecretDecryptError(f"unknown secret format {version!r}")
        raw = base64.b64decode(b64)
        return AESGCM(_key()).decrypt(raw[:12], raw[12:], aad.encode()).decode()
    except (ValueError, InvalidTag) as exc:
        if isinstance(exc, SecretDecryptError):
            raise
        raise SecretDecryptError("cannot decrypt stored secret") from exc


def aad_for(org_id: str, provider: str) -> str:
    return f"org-ai-key:{org_id}:{provider}"


def org_ai_keys(store, org_id: str) -> dict[str, str]:
    """Decrypted provider keys configured for an org ({} when none or when the feature is disabled)."""
    if not org_id or not enabled():
        return {}
    out: dict[str, str] = {}
    for row in store.list_ai_keys(org_id):
        try:
            out[row["provider"]] = decrypt(row["ciphertext"], aad_for(org_id, row["provider"]))
        except SecretDecryptError:
            continue  # e.g. ARCHFLOW_SECRET_KEY was rotated: fall back to the env keys
    return out
