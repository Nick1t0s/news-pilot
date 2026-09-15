from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings


@dataclass(slots=True)
class AppContext:
    """Shared runtime context injected into aiogram handlers."""

    cfg: Settings
    pool: object
    publisher: object
    sender: object
    pipeline: object = None
