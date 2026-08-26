"""JSON structured logging (stdlib only) + request-id support.

- Every log record is emitted as one JSON object per line.
- The current request id is injected from a contextvar set by the
  RequestIdMiddleware in app.main.
- Keys whose names look secret-ish are redacted from the `extra` payload.
"""

import json
import logging
import logging.config
from contextvars import ContextVar
from datetime import datetime, timezone

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)

_SECRET_HINTS = ("secret", "key", "token", "password", "authorization", "signature", "credential")
REDACTED = "***redacted***"


def _looks_secret(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in _SECRET_HINTS)


def redact(mapping: dict) -> dict:
    """Recursively redact values whose keys look secret-like."""
    out = {}
    for k, v in mapping.items():
        if _looks_secret(str(k)):
            out[k] = REDACTED
        elif isinstance(v, dict):
            out[k] = redact(v)
        elif isinstance(v, (list, tuple)):
            out[k] = [redact(i) if isinstance(i, dict) else i for i in v]
        else:
            out[k] = v
    return out


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = request_id_ctx.get()
        if rid:
            payload["request_id"] = rid
        extra = {
            k: v
            for k, v in record.__dict__.items()
            if k not in logging.LogRecord("", 0, "", 0, "", (), None).__dict__
            and k not in ("taskName",)
        }
        if extra:
            payload["extra"] = redact(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"json": {"()": "app.logging.JsonFormatter"}},
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "json",
                }
            },
            "root": {"level": level.upper(), "handlers": ["console"]},
        }
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
