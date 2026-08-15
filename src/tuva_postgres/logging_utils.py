"""Structured JSON logging: one JSON object per line, to stdout.

Every emitted line includes timestamp/level/event/run_id/environment/
app_version context via a `logging.LoggerAdapter`-free approach: callers
call `log_event(logger, "event_name", **fields)` rather than the usual
`logger.info("message")`, so every call site is forced to supply an
`event` name and structured fields instead of a free-text message that
might accidentally include secrets or PHI.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Any

from . import __version__

_TOKEN_LIKE_RE = re.compile(r"(bearer\s+)[A-Za-z0-9._\-]+", re.IGNORECASE)
_DSN_RE = re.compile(r"(postgres(?:ql)?://)[^@\s]+@", re.IGNORECASE)


def sanitize_text(text: str) -> str:
    """Best-effort redaction of secret-shaped substrings from free text
    that might end up in an exception message or log line. This is
    defense in depth -- call sites should not be relying on it as the
    only protection; they should never format secrets into messages at
    all."""
    if not text:
        return text
    text = _TOKEN_LIKE_RE.sub(r"\1***REDACTED***", text)
    text = _DSN_RE.sub(r"\1***REDACTED***@", text)
    return text


def sanitize_error(exc: BaseException) -> tuple[str, str]:
    """Return (category, sanitized_message) for any exception. Never
    includes a traceback (which could contain interpolated secrets from a
    library we don't control) -- only the exception's own str()."""
    category = getattr(exc, "category", exc.__class__.__name__)
    message = sanitize_text(str(exc))
    return category, message


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "app_version": __version__,
        }
        extra = getattr(record, "structured", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if "event" not in payload:
            payload["event"] = record.getMessage()
        return json.dumps(payload, default=str, sort_keys=True)


def configure_logging(level: str = "INFO", stream=None) -> logging.Logger:
    logger = logging.getLogger("tuva_postgres")
    logger.setLevel(level)
    logger.handlers.clear()
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    run_id: str | None = None,
    snapshot_id: str | None = None,
    stage: str | None = None,
    environment: str | None = None,
    duration_ms: float | None = None,
    table: str | None = None,
    error_category: str | None = None,
    error_message: str | None = None,
    **fields: Any,
) -> None:
    """Emit exactly one structured JSON log line for `event`."""
    structured: dict[str, Any] = {"event": event}
    if run_id is not None:
        structured["run_id"] = run_id
    if snapshot_id is not None:
        structured["snapshot_id"] = snapshot_id
    if stage is not None:
        structured["stage"] = stage
    if environment is not None:
        structured["environment"] = environment
    if duration_ms is not None:
        structured["duration_ms"] = round(duration_ms, 2)
    if table is not None:
        structured["table"] = table
    if error_category is not None:
        structured["error_category"] = error_category
    if error_message is not None:
        structured["error_message"] = sanitize_text(error_message)
    for key, value in fields.items():
        structured[key] = sanitize_text(value) if isinstance(value, str) else value
    logger.log(level, event, extra={"structured": structured})


class Stopwatch:
    """`with Stopwatch() as sw: ...` then `sw.elapsed_ms`."""

    def __enter__(self) -> "Stopwatch":
        self._start = time.monotonic()
        self.elapsed_ms = 0.0
        return self

    def __exit__(self, *exc_info) -> None:
        self.elapsed_ms = (time.monotonic() - self._start) * 1000.0
