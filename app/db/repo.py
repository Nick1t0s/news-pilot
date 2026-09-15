from __future__ import annotations

import datetime as dt

import asyncpg
import numpy as np

from app.db.entities import Counter, PublishedPost

_POST_COLUMNS = "id, source, text, embedding, tg_message_id, tg_url, published_at"

# Well-known counter keys (any other key works too — counters is a free-form key/value table).
COUNTER_DUPLICATES = "duplicates"
COUNTER_SKIPPED_UNIMPORTANT = "skipped_unimportant"
COUNTER_LIMIT_SKIPPED = "limit_skipped"
COUNTER_REJECTED = "rejected"
COUNTER_FAILED = "failed"
COUNTER_CLEARED = "cleared"


def _vec(value) -> np.ndarray | None:
    if value is None:
        return None
    return np.asarray(value, dtype=np.float32)


def _emb(value) -> list[float] | None:
    if value is None:
        return None
    return np.asarray(value, dtype=np.float32).tolist()


def _post(row) -> PublishedPost:
    return PublishedPost(
        id=row["id"],
        source=row["source"],
        text=row["text"],
        embedding=_emb(row["embedding"]),
        tg_message_id=row["tg_message_id"],
        tg_url=row["tg_url"],
        published_at=row["published_at"],
    )


# --- posts ---


async def insert_published_post(
    pool: asyncpg.Pool,
    *,
    source: str,
    text: str,
    tg_message_id: int,
    tg_url: str | None,
    embedding: list[float] | None,
) -> int:
    async with pool.acquire() as conn:
        post_id = await conn.fetchrow(
            "INSERT INTO posts (source, text, tg_message_id, tg_url, embedding)"
            " VALUES ($1, $2, $3, $4, $5) RETURNING id, published_at",
            source, text, tg_message_id, tg_url, _vec(embedding),
        )
    return int(post_id["id"])


async def nearest_posts(
    pool: asyncpg.Pool,
    embedding,
    *,
    cutoff: dt.datetime,
    limit: int,
) -> list[PublishedPost]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_POST_COLUMNS} FROM posts"
            " WHERE embedding IS NOT NULL AND published_at >= $1"
            " ORDER BY embedding <=> $2 LIMIT $3",
            cutoff, _vec(embedding), limit,
        )
    return [_post(row) for row in rows]


async def count_published_posts_since(pool: asyncpg.Pool, since: dt.datetime) -> int:
    async with pool.acquire() as conn:
        return int(await conn.fetchval(
            "SELECT COUNT(*) FROM posts WHERE published_at >= $1", since,
        ))


async def last_published_post_at(pool: asyncpg.Pool) -> dt.datetime | None:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT published_at FROM posts ORDER BY published_at DESC LIMIT 1",
        )


async def posts_by_source_since(pool: asyncpg.Pool, since: dt.datetime) -> list[Counter]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT source AS key, COUNT(*) AS value FROM posts WHERE published_at >= $1"
            " GROUP BY source ORDER BY value DESC",
            since,
        )
    return [Counter(key=row["key"], value=int(row["value"])) for row in rows]


# --- counters ---


async def counter_increment(pool: asyncpg.Pool, key: str, by: int = 1) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO counters (key, value) VALUES ($1, $2)"
            " ON CONFLICT (key) DO UPDATE SET value = counters.value + $2",
            key, by,
        )


async def counters_all(pool: asyncpg.Pool) -> list[Counter]:
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT key, value FROM counters ORDER BY key")
    return [Counter(key=row["key"], value=int(row["value"])) for row in rows]


async def counter_get(pool: asyncpg.Pool, key: str) -> int:
    async with pool.acquire() as conn:
        value = await conn.fetchval("SELECT value FROM counters WHERE key = $1", key)
    return int(value) if value is not None else 0
