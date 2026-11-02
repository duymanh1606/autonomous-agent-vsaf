"""Small, deterministic redaction helpers for operational logs."""

import hashlib
import re


_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\w)(?:\+?84|0)(?:[ .-]?\d){8,10}(?!\w)")
_SIGNED_QUERY = re.compile(
    r"([?&](?:token|signature|sig|auth|key|expires)=[^&\s]+)", re.IGNORECASE
)


def redact_pii(value) -> str:
    text = str(value or "")
    text = _EMAIL.sub("[REDACTED_EMAIL]", text)
    text = _PHONE.sub("[REDACTED_PHONE]", text)
    return _SIGNED_QUERY.sub("[REDACTED_QUERY]", text)


def content_fingerprint(value, length=12) -> str:
    """Non-reversible correlation id; never emits the original content."""
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:length]
