"""PII and secret redaction for everything sent to a model.

``llm._dispatch`` (the one place every model call goes through, Pydantic AI agents included) replaces personal
data and secrets in the messages with placeholders before the request leaves ArchFlow, and puts the personal data
back into the answer:

    alice@example.com          -> [EMAIL_1]     restored in the answer
    +1 (555) 123-4567          -> [PHONE_1]     restored
    4111 1111 1111 1111        -> [CARD_1]      restored (Luhn-checked)
    DE89 3704 0044 0532 0130 00 -> [IBAN_1]     restored (mod-97 checked)
    AKIA…, ghp_…, sk-…, xoxb-…, JWTs, PEM private keys, password=… -> [SECRET_1]   never restored

A secret never round-trips: the model only sees its placeholder and the answer keeps it. Answers are scanned for
secrets too (``mask_secrets``); one the model wrote itself is masked before it reaches the caller.

The same value gets the same placeholder within one request, so the model can still tell two addresses apart.
Only counts per category are reported (``observe``), never the values.

Environment:
    ARCHFLOW_REDACT   on (default: PII and secrets) | secrets (secrets only) | off
"""

from __future__ import annotations

import logging
import os
import re
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Iterator

log = logging.getLogger("archflow.redact")

__all__ = ["mode", "Redactor", "redact", "restore", "mask_secrets", "StreamRestorer", "observe", "report"]


def mode() -> str:
    m = os.environ.get("ARCHFLOW_REDACT", "on").strip().lower()
    if m in {"off", "0", "false", "no"}:
        return "off"
    return "secrets" if m == "secrets" else "on"


# --------------------------------------------------------------------------- validators
def luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


# Card networks by prefix, with their lengths (Visa, Mastercard, Amex, Diners, JCB, Discover/UnionPay/Maestro).
_CARD_LENGTHS = ((re.compile(r"4"), {13, 16, 19}), (re.compile(r"5[1-5]|2[2-7]"), {16}), (re.compile(r"3[47]"), {15}),
                 (re.compile(r"3[0689]"), {14, 15, 16, 17, 18, 19}), (re.compile(r"35"), {16, 17, 18, 19}),
                 (re.compile(r"6"), {16, 17, 18, 19}))


def _card(raw: str) -> bool:
    digits = re.sub(r"[ -]", "", raw)
    if len(set(digits)) == 1 or not luhn(digits):
        return False
    return any(rx.match(digits) and len(digits) in lengths for rx, lengths in _CARD_LENGTHS)


def _iban(raw: str) -> bool:
    s = raw.replace(" ", "").upper()
    if not 15 <= len(s) <= 34:
        return False
    moved = s[4:] + s[:4]
    return int("".join(str(int(c, 36)) for c in moved)) % 97 == 1


def _phone(raw: str) -> bool:
    return 8 <= len(re.sub(r"\D", "", raw)) <= 15


# Values after ``password=`` that are not secrets: references (${X}, <your key>), masks, and words or type and
# attribute names without digits ('string', 'required', 'Optional[str]', 'settings.api_key'); 'hunter22' is one.
_NOT_A_VALUE = re.compile(r"^(?:[$<{\[%*#]|\.\.\.|x{3,}$)", re.I)
_WORDISH = re.compile(r"[A-Za-z_.\[\]()<>|-]+")


def _assigned(raw: str) -> bool:
    if len(raw) < 4 or _NOT_A_VALUE.match(raw):
        return False
    return not (_WORDISH.fullmatch(raw) and len(raw) < 24)


# --------------------------------------------------------------------------- patterns
@dataclass(frozen=True)
class Pattern:
    kind: str                           # count category (trace metadata)
    label: str                          # placeholder label
    regex: re.Pattern[str]
    secret: bool
    check: Callable[[str], bool] | None = None
    group: int = 0                      # the group that holds the value (the rest of the match is kept)


_KEY_NAME = (r"(?:[A-Za-z0-9]+[_-])*(?:password|passwd|pwd|passphrase|secret|api[_-]?key|apikey|access[_-]?key|"
             r"secret[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token|client[_-]?secret|token)")

# Secrets first (a JWT or a PEM block contains things that look like other data), then structured PII.
SECRETS: tuple[Pattern, ...] = (
    Pattern("private_key", "SECRET", re.compile(
        r"-----BEGIN[A-Z ]*PRIVATE KEY-----.*?-----END[A-Z ]*PRIVATE KEY-----", re.S), True),
    Pattern("jwt", "SECRET", re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"), True),
    Pattern("aws_key", "SECRET", re.compile(
        r"\b(?:AKIA|ASIA|ABIA|ACCA|AGPA|AIDA|AROA|ANPA|ANVA|APKA)[0-9A-Z]{16}\b"), True),
    Pattern("github_token", "SECRET", re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})\b"), True),
    Pattern("api_key", "SECRET", re.compile(r"(?<![A-Za-z0-9])sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"), True),
    Pattern("slack_token", "SECRET", re.compile(r"\bxox[abposre]-[A-Za-z0-9-]{10,}"), True),
    Pattern("bearer_token", "SECRET", re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/=-]{20,})"), True, group=1),
    Pattern("password", "SECRET", re.compile(
        r"(?i)\b" + _KEY_NAME + r"""["']?\s*[:=]\s*["']?([^\s"'`,;)}\]]+)"""), True, _assigned, group=1),
)

PII: tuple[Pattern, ...] = (
    Pattern("email", "EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"),
            False),
    Pattern("iban", "IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b"), False, _iban),
    Pattern("credit_card", "CARD", re.compile(r"(?<![\w-])\d(?:[ -]?\d){12,18}(?![\w-])"), False, _card),
    Pattern("phone", "PHONE", re.compile(
        r"(?<![\w+])(?:\+\d{1,3}(?:[ .-]?\(?\d{1,4}\)?){2,5}"   # +44 20 7946 0958, +1 (555) 123-4567
        r"|\(\d{3}\) ?\d{3}[ .-]\d{4}"                          # (555) 123-4567
        r"|\d{3}[.-]\d{3}[.-]\d{4})(?![\w])"), False, _phone),  # 555-123-4567
)

_PLACEHOLDER = re.compile(r"\[(EMAIL|PHONE|CARD|IBAN|SECRET)_(\d+)\]")
MASK = "[REDACTED_SECRET]"


# --------------------------------------------------------------------------- redaction
class Redactor:
    """Redacts several texts with one shared placeholder numbering (one request = one Redactor).

    ``mapping`` holds the restorable placeholders (PII only); ``counts`` the replacements per category."""

    def __init__(self, mode_: str | None = None):
        self.mode = mode_ or mode()
        self.mapping: dict[str, str] = {}
        self.counts: Counter[str] = Counter()
        self._seen: dict[tuple[str, str], str] = {}
        self._next: Counter[str] = Counter()

    def _patterns(self) -> tuple[Pattern, ...]:
        if self.mode == "off":
            return ()
        return SECRETS if self.mode == "secrets" else SECRETS + PII

    def _placeholder(self, p: Pattern, value: str) -> str:
        key = (p.label, value)
        if key not in self._seen:
            self._next[p.label] += 1
            ph = f"[{p.label}_{self._next[p.label]}]"
            self._seen[key] = ph
            if not p.secret:
                self.mapping[ph] = value
        return self._seen[key]

    def redact(self, text: str) -> str:
        if not text or not isinstance(text, str):
            return text
        for p in self._patterns():
            def swap(m: re.Match[str], p: Pattern = p) -> str:
                value = m.group(p.group)
                if _PLACEHOLDER.fullmatch(value) or (p.check is not None and not p.check(value)):
                    return m.group(0)
                self.counts[p.kind] += 1
                ph = self._placeholder(p, value)
                if p.group == 0:
                    return ph
                start, end = m.start(p.group) - m.start(), m.end(p.group) - m.start()
                return m.group(0)[:start] + ph + m.group(0)[end:]
            text = p.regex.sub(swap, text)
        return text

    def redact_value(self, value: Any) -> Any:
        """Strings inside dicts and lists (tool-call arguments) too."""
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {k: self.redact_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact_value(v) for v in value]
        return value


def redact(text: str, mode_: str | None = None) -> tuple[str, dict[str, str]]:
    """(redacted text, {placeholder: original}) — the mapping holds PII only, so secrets cannot be restored."""
    r = Redactor(mode_)
    return r.redact(text), dict(r.mapping)


def restore(text: str, mapping: dict[str, str]) -> str:
    """Put PII back in place of its placeholders; unknown placeholders (and every secret) stay as they are."""
    if not text or not mapping or not isinstance(text, str):
        return text
    return _PLACEHOLDER.sub(lambda m: mapping.get(m.group(0), m.group(0)), text)


def restore_value(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, str):
        return restore(value, mapping)
    if isinstance(value, dict):
        return {k: restore_value(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [restore_value(v, mapping) for v in value]
    return value


def mask_secrets(text: str, counts: Counter[str] | None = None) -> str:
    """Replace secrets in model output with ``[REDACTED_SECRET]`` (counted into ``counts``)."""
    if not text or not isinstance(text, str):
        return text
    for p in SECRETS:
        def swap(m: re.Match[str], p: Pattern = p) -> str:
            value = m.group(p.group)
            if value == MASK or _PLACEHOLDER.fullmatch(value) or (p.check is not None and not p.check(value)):
                return m.group(0)
            if counts is not None:
                counts[p.kind] += 1
            if p.group == 0:
                return MASK
            start, end = m.start(p.group) - m.start(), m.end(p.group) - m.start()
            return m.group(0)[:start] + MASK + m.group(0)[end:]
        text = p.regex.sub(swap, text)
    return text


def mask_value(value: Any, counts: Counter[str] | None = None) -> Any:
    if isinstance(value, str):
        return mask_secrets(value, counts)
    if isinstance(value, dict):
        return {k: mask_value(v, counts) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_value(v, counts) for v in value]
    return value


# --------------------------------------------------------------------------- streams
_KEY_TAIL = re.compile(r"(?i)\b" + _KEY_NAME + r"""["']?\s*[:=]?\s*["']?$""")
_PEM_OPEN = re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----")
_PEM_CLOSE = re.compile(r"-----END[A-Z ]*PRIVATE KEY-----")


class StreamRestorer:
    """Restores PII and masks secrets in streamed text. A placeholder or a secret can be split across chunks, so
    the text after the last whitespace (and an unfinished private key block) is held back until it is complete."""

    def __init__(self, mapping: dict[str, str], mask: bool = True, hold_chars: int = 4096,
                 hold_block_chars: int = 16000):
        self.mapping, self.mask = mapping, mask
        self.hold_chars, self.hold_block_chars = hold_chars, hold_block_chars
        self.masked: Counter[str] = Counter()
        self._buf = ""

    def _out(self, text: str) -> str:
        text = restore(text, self.mapping)
        return mask_secrets(text, self.masked) if self.mask else text

    def _cut(self) -> int:
        buf = self._buf
        cut = max(buf.rfind(" "), buf.rfind("\n"), buf.rfind("\t")) + 1
        if cut == 0:
            return len(buf) if len(buf) > self.hold_chars else 0
        opened = None
        for m in _PEM_OPEN.finditer(buf):
            opened = m
        if opened and not _PEM_CLOSE.search(buf, opened.end()) and len(buf) - opened.start() <= self.hold_block_chars:
            cut = min(cut, opened.start())
        tail = _KEY_TAIL.search(buf, 0, cut)
        if tail and tail.end() == cut:  # 'password = ' sent, its value not yet
            cut = tail.start()
        return cut

    def feed(self, text: str) -> str:
        self._buf += text
        cut = self._cut()
        out, self._buf = self._buf[:cut], self._buf[cut:]
        return self._out(out) if out else ""

    def flush(self) -> str:
        out, self._buf = self._buf, ""
        return self._out(out) if out else ""


# --------------------------------------------------------------------------- reporting (traces)
RedactionObserver = Callable[[dict[str, Any]], None]
_observers: ContextVar[tuple[RedactionObserver, ...]] = ContextVar("archflow_redact_observers", default=())


@contextmanager
def observe(observer: RedactionObserver) -> Iterator[None]:
    """Receive {"redacted": {category: n}, "masked": {...}, "injection": [...]} for model calls in this context."""
    token = _observers.set((*_observers.get(), observer))
    try:
        yield
    finally:
        _observers.reset(token)


def report(event: dict[str, Any]) -> None:
    if not any(event.values()):
        return
    for fn in _observers.get():
        try:
            fn(event)
        except Exception:  # noqa: BLE001 - reporting must never fail a model call
            log.exception("redaction observer failed")
