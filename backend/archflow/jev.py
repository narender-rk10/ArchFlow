"""Client for the Jev decision API (https://thejevai.com/docs), the first engine behind "Compare drafts".

Jev answers structured questions about a ``state``: one ``choice`` question ("which alternative best satisfies the
requirements?") and one ``score`` question per (criterion, alternative) on a 5-level scale. ``evaluation.py``
builds the questions, runs Jev next to the LLM explainer, and falls back to the LLM, then an offline heuristic.

Environment: JEV_API_KEY, JEV_API_URL (default https://thejevai.com/v1/systemone), JEV_MODEL (jev-latest),
JEV_TIMEOUT seconds (60), JEV_MAX_ATTEMPTS (4), JEV_BACKOFF_BASE (0.5), JEV_BACKOFF_CAP (20).
The API key is never logged.
"""

from __future__ import annotations

import email.utils
import logging
import os
import random
import time
from typing import Any, Callable

import httpx

log = logging.getLogger("archflow.jev")

DEFAULT_URL = "https://thejevai.com/v1/systemone"


class JevError(RuntimeError):
    """The Jev API failed or returned something unusable."""


class JevUnavailable(JevError):
    """Jev is not configured (no JEV_API_KEY)."""


def available() -> bool:
    return bool(os.environ.get("JEV_API_KEY", "").strip())


def build_questions(keys: dict[str, str], criteria: dict[str, str], scale: list[str]) -> dict[str, Any]:
    """``keys`` maps option keys (A, B, ...) to branch names."""
    questions: dict[str, Any] = {"best": {
        "type": "choice",
        "instructions": "Which alternative architecture best satisfies the requirements, including the "
                        "non-functional requirements (NFRs)? Weigh critical and high-priority requirements most.",
        "criteria": {k: f"Alternative {k} (branch '{b}')" for k, b in keys.items()},
    }}
    for crit, desc in criteria.items():
        for k, b in keys.items():
            questions[f"score_{crit}_{k}"] = {
                "type": "score", "criteria": scale,
                "instructions": f"Rate alternative {k} (branch '{b}') on {desc}, given the requirements.",
            }
    return questions


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        parsed = email.utils.parsedate_tz(value)
        return max(0.0, email.utils.mktime_tz(parsed) - time.time()) if parsed else None


def _retryable(status: int) -> bool:
    return status in (408, 429, 529) or status >= 500


def call(state: dict[str, Any], questions: dict[str, Any], *, api_key: str | None = None, url: str | None = None,
         transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep,
         max_attempts: int | None = None) -> dict[str, Any]:
    """POST to Jev with retries (exponential backoff with jitter, ``Retry-After``) on 408/429/529/5xx and
    network errors. Returns the response (with ``answers``); raises JevUnavailable / JevError."""
    key = api_key or os.environ.get("JEV_API_KEY", "").strip()
    if not key:
        raise JevUnavailable("JEV_API_KEY is not set")
    url = url or os.environ.get("JEV_API_URL", DEFAULT_URL)
    attempts = max(1, max_attempts or int(os.environ.get("JEV_MAX_ATTEMPTS", 4)))
    base, cap = float(os.environ.get("JEV_BACKOFF_BASE", 0.5)), float(os.environ.get("JEV_BACKOFF_CAP", 20))
    payload = {"state": state, "model": os.environ.get("JEV_MODEL", "jev-latest"), "questions": questions}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    last = ""
    with httpx.Client(timeout=float(os.environ.get("JEV_TIMEOUT", 60)), transport=transport) as client:
        for attempt in range(attempts):
            wait: float | None = None
            try:
                resp = client.post(url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                last = f"network error: {type(exc).__name__}"
            else:
                if resp.status_code < 300:
                    try:
                        data = resp.json()
                    except ValueError as exc:
                        raise JevError("Jev returned invalid JSON") from exc
                    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
                        raise JevError("Jev response has no 'answers' object")
                    return data
                last = f"HTTP {resp.status_code}"
                if not _retryable(resp.status_code):
                    try:
                        detail = str(resp.json().get("message") or "")[:200]
                    except ValueError:
                        detail = resp.text[:200]
                    raise JevError(f"Jev request failed: HTTP {resp.status_code} {detail}".strip())
                wait = _retry_after(resp)
            if attempt + 1 < attempts:
                delay = min(cap, wait) if wait is not None else random.uniform(0, min(cap, base * 2 ** attempt))
                log.info("jev attempt %d failed (%s); retrying in %.2fs", attempt + 1, last, delay)
                sleep(delay)
    raise JevError(f"Jev request failed after {attempts} attempts ({last})")


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse(resp: dict[str, Any], keys: dict[str, str], criteria: list[str]) -> dict[str, Any]:
    """{"recommended": branch | None, "scores": {branch: {criterion: 1..5}}, "probabilities": {branch: p},
    "confidence": float | None, "usage": ...}. Raises JevError when nothing usable came back."""
    answers = resp.get("answers") or {}
    best = answers.get("best") or {}
    choice = best.get("choice")
    if choice not in keys:  # tolerate an answer that names the branch instead of the option key
        choice = {b: k for k, b in keys.items()}.get(choice)
    probabilities = {keys[k]: round(p, 3) for k, v in (best.get("probabilities") or {}).items()
                     if k in keys and (p := _num(v)) is not None}
    scores: dict[str, dict[str, float]] = {b: {} for b in keys.values()}
    for crit in criteria:
        for k, b in keys.items():
            s = _num((answers.get(f"score_{crit}_{k}") or {}).get("score"))
            if s is not None:  # Jev scores are 0-based indexes into the scale levels; ours is 1..5
                scores[b][crit] = round(min(5.0, max(1.0, s + 1)), 2)
    if not any(scores.values()) and choice is None:
        raise JevError("Jev response contained no usable answers")
    conf = _num(best.get("confidence"))
    return {"recommended": keys[choice] if choice else None, "scores": scores, "probabilities": probabilities,
            "confidence": round(conf, 3) if conf is not None else None,
            "usage": resp.get("usage"), "model": resp.get("model"), "elapsed": resp.get("elapsed")}
