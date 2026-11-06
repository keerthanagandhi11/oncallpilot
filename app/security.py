"""Signed acknowledgement links, API token checks, and masking of secrets and phone numbers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time

_SECRET_PATTERNS = [
    # key=value or key: value where the key looks secret
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret)\b(\s*[:=]\s*)"
            r"(\"[^\"]*\"|'[^']*'|\S+)"
        ),
        r"\1\2****",
    ),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 ****"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA****"),
    (re.compile(r"(?i)(://[^:/\s]+:)[^@\s]+@"), r"\1****@"),  # user:pass@host in URLs
    (re.compile(r"\b(sk|gsk|xox[abp])[-_][A-Za-z0-9_-]{16,}\b"), r"\1-****"),
]


def redact(text: str | None) -> str:
    """Mask passwords, tokens and keys before text leaves the service (LLM, Teams, email)."""
    if not text:
        return ""
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def mask_phone(phone: str | None) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 4:
        return "****"
    return f"{'+' if (phone or '').strip().startswith('+') else ''}{'*' * (len(digits) - 4)}{digits[-4:]}"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def sign_ack_token(secret: str, incident_id: str, who: str, ttl_seconds: int, now: float | None = None) -> str:
    payload = {"i": incident_id, "w": who, "e": int((now or time.time()) + ttl_seconds)}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


class InvalidToken(Exception):
    pass


def verify_ack_token(secret: str, token: str, now: float | None = None) -> dict:
    try:
        body, sig = token.split(".", 1)
        expected = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            raise InvalidToken("bad signature")
        payload = json.loads(_unb64(body))
    except InvalidToken:
        raise
    except Exception as exc:
        raise InvalidToken("malformed token") from exc
    if payload.get("e", 0) < (now or time.time()):
        raise InvalidToken("link expired")
    return payload


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def calls_webhook_token(secret_key: str) -> str:
    """Token in the Graph call callback URL, so only Graph (which we gave the URL) can post call states."""
    return hashlib.sha256(f"{secret_key}:teams-calls".encode()).hexdigest()[:32]
