from __future__ import annotations

import logging
import sys


class ConsoleFormatter(logging.Formatter):
    """`LOGGER: message` — human-readable single-line format."""

    def format(self, record: logging.LogRecord) -> str:
        line = f"{record.name.upper()}: {record.getMessage()}"
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
