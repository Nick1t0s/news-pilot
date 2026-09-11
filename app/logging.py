from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from contextvars import ContextVar

news_id_var: ContextVar = ContextVar("news_id", default=None)


def set_news_id(value: int | None) -> None:
    news_id_var.set(value)


def get_news_id() -> int | None:
    return news_id_var.get()


_EXTRA_KEYS = ("stage", "source", "feed", "post_id", "attempt", "tool")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = dt.datetime.fromtimestamp(record.created, tz=dt.timezone.utc).isoformat(timespec="milliseconds")
        payload: dict = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        news_id = news_id_var.get()
        if news_id is not None:
            payload["news_id"] = news_id
        for key in _EXTRA_KEYS:
            value = record.__dict__.get(key)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "aiosqlite", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
