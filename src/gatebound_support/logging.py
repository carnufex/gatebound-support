"""Structured JSON-lines logging to stdout.

Never log tokens, identity JWTs, or Discord secrets — only names, ids, statuses and timings.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

_LOGGER_NAME = "gatebound_support"


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLineFormatter())
    root.handlers = [handler]


def get_logger(name: str = _LOGGER_NAME) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, message: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, message, extra={"fields": fields})


def log_tool_call(name: str, duration_ms: float, status: str, conversation_id: str | None) -> None:
    """Every MCP tool call: name, duration, status, conversation id. Nothing else, ever."""
    log_event(
        get_logger("gatebound_support.mcp"),
        "tool_call",
        tool=name,
        duration_ms=round(duration_ms, 2),
        status=status,
        conversation_id=conversation_id or None,
    )
