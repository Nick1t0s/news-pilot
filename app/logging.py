from __future__ import annotations

import logging
import sys
from contextvars import ContextVar

news_id_var: ContextVar = ContextVar("news_id", default=None)


def set_news_id(value: int | None) -> None:
    news_id_var.set(value)


def get_news_id() -> int | None:
    return news_id_var.get()


class ConsoleFormatter(logging.Formatter):
    """`LOGGER: message [news_id=N]` — human-readable single-line format."""

    def format(self, record: logging.LogRecord) -> str:
        line = f"{record.name.upper()}: {record.getMessage()}"
        news_id = news_id_var.get()
        if news_id is not None:
            line += f" [news_id={news_id}]"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ConsoleFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "aiosqlite", "asyncio", "aiogram.event", "aiogram.dispatcher"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # trafilatura logs "discarding data"/"cannot get HTML" for every page it fails to
    # parse; we fall back to the RSS summary, so this noise is not actionable
    logging.getLogger("trafilatura").setLevel(logging.CRITICAL)
