"""Structured logging.

CloudWatch Logs is the only debugging surface a Lambda has, so lines are emitted
as JSON: greppable in the console, and queryable with Logs Insights without
regex.

Every line carries the invocation's context — stack ID, dedupe key, report ID —
so one deployment's trail can be isolated from everything else happening
concurrently. Context lives in a :class:`~contextvars.ContextVar` rather than a
module global, so concurrent work cannot leak fields between invocations.

Named ``logs`` rather than ``logging`` deliberately. A module called ``logging``
inside a package shadows the standard library for any tool that resolves the
directory as a namespace package, which is exactly the sort of ambiguity that
produces a confusing import error on first deploy.

Nothing here logs a secret. The Slack credential is read from Secrets Manager
straight into the request that uses it and never passes through a log record.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

#: Defaults to None rather than {} — a mutable default is shared across every
#: context that has not set one, so anything mutating it in place would leak
#: fields between invocations.
_context: ContextVar[dict[str, Any] | None] = ContextVar("log_context", default=None)

#: Record attributes that belong to logging itself rather than to the event.
_STANDARD_ATTRIBUTES = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class JsonFormatter(logging.Formatter):
    """Renders records as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        payload.update(_context.get() or {})

        # Anything passed via `extra=` lands on the record as a custom attribute.
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRIBUTES and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_logging(level: str | None = None) -> None:
    """Install the JSON formatter on the root logger.

    Lambda pre-configures a handler on the root logger, so its formatter is
    replaced rather than a second handler added — otherwise every line is emitted
    twice.
    """
    resolved = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    root = logging.getLogger()
    root.setLevel(resolved)

    if root.handlers:
        for handler in root.handlers:
            handler.setFormatter(JsonFormatter())
    else:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)

    # boto3 at DEBUG buries everything else in wire traffic.
    for noisy in ("boto3", "botocore", "urllib3", "s3transfer"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Attach fields to every log line emitted inside the block.

    Nests: inner fields are merged over outer ones, and the previous context is
    restored on exit even if the block raises.
    """
    merged = {
        **(_context.get() or {}),
        **{k: v for k, v in fields.items() if v is not None},
    }
    token = _context.set(merged)
    try:
        yield
    finally:
        _context.reset(token)


def current_context() -> dict[str, Any]:
    """The context that would be attached to a line logged right now."""
    return dict(_context.get() or {})
