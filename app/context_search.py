from __future__ import annotations

import datetime as dt
import logging

import asyncpg

from app.config import Settings
from app.db import repo
from app.db.entities import FeedItem, PublishedPost
from app.textutil import cosine_similarity

log = logging.getLogger("context")


class ContextSearch:
    """Finds previously published channel posts relevant to a news item."""

    def __init__(self, cfg: Settings, pool: asyncpg.Pool, embeddings) -> None:
        self._cfg = cfg
        self._pool = pool
        self._embeddings = embeddings

    async def find(self, item: FeedItem, embedding: list[float] | None = None) -> list[PublishedPost]:
        """Search published posts by embedding. The embedding may be passed in
        (computed earlier by the dedup gate) to avoid a second embed call."""
        if embedding is None:
            embedding = await self._embeddings.embed(f"{item.title}\n{item.text[: self._cfg.embeddings.max_chars]}")
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=self._cfg.context.window_days)
        rows = await repo.nearest_posts(
            self._pool,
            embedding,
            cutoff=cutoff,
            limit=self._cfg.context.top_k,
        )
        min_similarity = self._cfg.context.min_similarity
        return [post for post in rows if cosine_similarity(embedding, post.embedding) >= min_similarity]
