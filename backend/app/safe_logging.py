import logging
import re
from typing import Any

# Explicit allowlist of diagnostic fields permitted in application logs.
ALLOWED_LOG_FIELDS: frozenset[str] = frozenset(
    {
        "event",
        "channel",
        "outcome",
        "reason_code",
        "session_id",
        "hermes_session_id",
        "update_id",
        "tool_name",
        "duration_ms",
        "status_code",
        "has_text",
        "has_image",
        "text_length",
        "row_count",
        "affected_rows",
        "limit",
        "offset",
        "match_count",
        "ambiguous",
        "stop_reason",
        "attempt",
    }
)

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"bot\d+:[A-Za-z0-9_-]+"), "bot<REDACTED>"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]+"), "Bearer <REDACTED>"),
    (re.compile(r"sk-[A-Za-z0-9_-]{10,}"), "sk-<REDACTED>"),
)


def redact_secrets(value: str) -> str:
    cleaned = value
    for pattern, replacement in _SECRET_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    text = redact_secrets(str(value))
    # Guard against accidental payload dumps in identifier/code fields.
    if len(text) > 128:
        return text[:128] + "..."
    return text


def sanitize_fields(fields: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in ALLOWED_LOG_FIELDS or value is None:
            continue
        sanitized[key] = _sanitize_value(value)
    return sanitized


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    channel: str,
    outcome: str,
    reason_code: str | None = None,
    **fields: Any,
) -> None:
    payload = sanitize_fields(
        {
            "event": event,
            "channel": channel,
            "outcome": outcome,
            "reason_code": reason_code,
            **fields,
        }
    )
    formatted = " ".join(f"{k}={v}" for k, v in payload.items())
    logger.log(level, formatted)
