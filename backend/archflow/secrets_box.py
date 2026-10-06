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
import os
import secrets

PROVIDERS = ("gemini", "anthropic", "openai")
_VERSION = "v1"


class SecretsUnavailable(RuntimeError):
    """ARCHFLOW_SECRET_KEY is not set (or the cryptography package is missing)."""


class SecretDecryptError(ValueError):
    """The ciphertext is corrupt, bound to another row, or was encrypted under a different key."""


def _master() -> str:
    return os.environ.get("ARCHFLOW_SECRET_KEY", "").strip()


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
