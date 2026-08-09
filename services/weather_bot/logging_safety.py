"""Helpers for useful diagnostics without retaining raw user messages."""
from __future__ import annotations

import hashlib
import re


_BEARER_SECRET_RE = re.compile(r"(?i)\bBearer\s+[^\s，,；;]+")
_LABELED_SECRET_RE = re.compile(
    r"(?i)(?:password|passwd|token|api[_-]?key|app[_-]?secret|authorization|密码|口令|密钥)"
    r"\s*[:=：]\s*[^\s，,；;]+"
)
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_POSITION_RE = re.compile(
    r"(?i)(?:持仓|仓位)\s*[-+]?\d+(?:\.\d+)?\s*(?:MW|MWh|兆瓦|兆瓦时|万千瓦时)?"
)


def text_log_metadata(text: object) -> dict[str, int | str]:
    """Return non-content metadata suitable for structured application logs."""

    value = text if isinstance(text, str) else str(text or "")
    fingerprint = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return {
        "text_length": len(value),
        "text_sha256": fingerprint,
    }


def safe_error_summary(error: BaseException) -> str:
    """Return only an exception class; messages can contain URLs or credentials."""

    return type(error).__name__


def redact_sensitive_text(text: object) -> str:
    """Redact common credentials and personal/trading values before persistence."""

    value = text if isinstance(text, str) else str(text or "")
    value = _BEARER_SECRET_RE.sub("Bearer [REDACTED_CREDENTIAL]", value)
    value = _LABELED_SECRET_RE.sub("[REDACTED_CREDENTIAL]", value)
    value = _PHONE_RE.sub("[REDACTED_PHONE]", value)
    return _POSITION_RE.sub("持仓[REDACTED_POSITION]", value)
