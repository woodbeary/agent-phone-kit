"""Verify the ElevenLabs-Signature header on post-call webhooks.

The scheme, as implemented by ElevenLabs' own Python SDK
(``elevenlabs/webhooks_custom.py``, ``construct_event``), which the post-call
webhook docs tell you to use:

* header:     ``ElevenLabs-Signature: t=<unix seconds>,v0=<hex digest>``
* digest:     HMAC-SHA256 of ``"<t>.<raw request body>"`` keyed with the webhook secret
* freshness:  requests whose ``t`` is more than 30 minutes old are rejected

This module adds three things the SDK does not do: a constant-time comparison,
rejection of timestamps more than five minutes in the future, and a configurable
tolerance. It works on the raw body bytes, so verify before you parse the JSON.
"""

import hashlib
import hmac
import re
import time

SIGNATURE_HEADER = "ElevenLabs-Signature"
DEFAULT_TOLERANCE_SECONDS = 30 * 60
MAX_FUTURE_SKEW_SECONDS = 5 * 60

_TIMESTAMP = re.compile(r"[0-9]{1,12}")


class SignatureError(ValueError):
    """The request is not a fresh, authentic ElevenLabs webhook."""


def compute_signature(secret: str, timestamp: int | str, body: bytes) -> str:
    """Return the hex HMAC-SHA256 digest ElevenLabs sends as ``v0``."""
    message = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def sign_payload(secret: str, body: bytes, *, timestamp: int | None = None) -> str:
    """Build an ``ElevenLabs-Signature`` header value for ``body``.

    Useful in tests and for replaying a stored payload against a staging server.
    """
    ts = int(time.time()) if timestamp is None else timestamp
    return f"t={ts},v0={compute_signature(secret, ts, body)}"


def verify_signature(
    body: bytes,
    header: str | None,
    secret: str,
    *,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: float | None = None,
) -> int:
    """Check ``header`` against ``body``; return the signed timestamp.

    Raises ``SignatureError`` if the header is missing or malformed, the
    timestamp is stale or too far in the future, or no ``v0`` digest matches.
    """
    if not secret:
        raise ValueError("webhook secret is not configured")
    if not header:
        raise SignatureError(f"missing {SIGNATURE_HEADER} header")

    timestamp_text: str | None = None
    candidates: list[str] = []
    for part in header.split(","):
        key, sep, value = part.strip().partition("=")
        if not sep:
            continue
        if key == "t":
            timestamp_text = value
        elif key == "v0":
            candidates.append(value)

    if timestamp_text is None or not candidates:
        raise SignatureError("header needs both t=<timestamp> and v0=<signature>")
    if not _TIMESTAMP.fullmatch(timestamp_text):
        raise SignatureError("timestamp is not a unix time in seconds")

    current = time.time() if now is None else now
    age = current - int(timestamp_text)
    if age > tolerance_seconds:
        raise SignatureError(f"timestamp is {int(age)} s old; the limit is {tolerance_seconds} s")
    if -age > MAX_FUTURE_SKEW_SECONDS:
        raise SignatureError("timestamp is in the future")

    # Sign the timestamp exactly as received, as the SDK does.
    expected = compute_signature(secret, timestamp_text, body).encode()
    if not any(hmac.compare_digest(expected, c.encode()) for c in candidates):
        raise SignatureError("signature does not match")
    return int(timestamp_text)
