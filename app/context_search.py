from __future__ import annotations

import datetime as dt
import logging

import asyncpg

from app.config import Settings
from app.db import repo
from app.db.entities import News, Post
from app.providers.embeddings import EmbeddingProvider
from app.textutil import cosine_similarity

log = logging.getLogger("context")


class ContextSearch:
    """Finds previously published channel posts relevant to a news item."""

    def __init__(self, cfg: Settings, pool: asyncpg.Pool, embeddings: EmbeddingProvider) -> None:
        self._cfg = cfg
        self._pool = pool
        self._embeddings = embeddings

    async def find(self, news: News) -> list[Post]:
        embedding = await self._embeddings.embed(f"{news.title}\n{news.text[: self._cfg.embeddings.max_chars]}")
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=self._cfg.context.window_days)
        rows = await repo.nearest_published_posts(
            self._pool,
            embedding,
            cutoff=cutoff,
            limit=self._cfg.context.top_k,
        )
        min_similarity = self._cfg.context.min_similarity
        return [post for post in rows if cosine_similarity(embedding, post.embedding) >= min_similarity]
