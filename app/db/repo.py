from __future__ import annotations

import datetime as dt

import asyncpg
import numpy as np

from app.db.entities import (
    News,
    NewsStatus,
    Post,
    PostImage,
    PostStatus,
)

_NEWS_COLUMNS = "id, source, external_id, title, text, url, full_text_fetched, published_at, embedding, status, duplicate_of_id, created_at"
_POST_COLUMNS = "id, news_id, text, embedding, tg_message_id, tg_url, status, published_at, created_at"


def _vec(value) -> np.ndarray | None:
    if value is None:
        return None
    return np.asarray(value, dtype=np.float32)


def _emb(value) -> list[float] | None:
    if value is None:
        return None
    return np.asarray(value, dtype=np.float32).tolist()


def _news(row) -> News:
    return News(
        id=row["id"],
        source=row["source"],
        external_id=row["external_id"],
        title=row["title"],
        text=row["text"],
        url=row["url"],
        full_text_fetched=row["full_text_fetched"],
        published_at=row["published_at"],
        embedding=_emb(row["embedding"]),
        status=NewsStatus(row["status"]),
        duplicate_of_id=row["duplicate_of_id"],
        created_at=row["created_at"],
    )


def _post(row) -> Post:
    return Post(
        id=row["id"],
        news_id=row["news_id"],
        text=row["text"],
        embedding=_emb(row["embedding"]),
        tg_message_id=row["tg_message_id"],
        tg_url=row["tg_url"],
        status=PostStatus(row["status"]),
        published_at=row["published_at"],
        created_at=row["created_at"],
    )


# --- news ---


async def news_exists(pool: asyncpg.Pool, source: str, external_id: str) -> bool:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT 1 FROM news WHERE source = $1 AND external_id = $2", source, external_id
        ) is not None


async def add_news(
    pool: asyncpg.Pool,
    *,
    source: str,
    external_id: str,
    title: str,
    text: str,
    url: str,
    full_text_fetched: bool,
    published_at: dt.datetime | None,
    status: NewsStatus,
    log_stage_name: str | None = None,
    log_message: str | None = None,
) -> int:
    async with pool.acquire() as conn, conn.transaction():
        news_id = await conn.fetchval(
            "INSERT INTO news (source, external_id, title, text, url, full_text_fetched,"
            " published_at, status)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id",
            source,
            external_id,
            title,
            text,
            url,
            full_text_fetched,
            published_at,
            status.value,
        )
        if log_stage_name and log_message:
            await _log(conn, news_id, log_stage_name, "info", log_message)
        return int(news_id)


async def get_news(pool: asyncpg.Pool, news_id: int) -> News | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(f"SELECT {_NEWS_COLUMNS} FROM news WHERE id = $1", news_id)
    return _news(row) if row else None


async def latest_news_id(pool: asyncpg.Pool) -> int | None:
    async with pool.acquire() as conn:
        value = await conn.fetchval("SELECT id FROM news ORDER BY id DESC LIMIT 1")
    return int(value) if value is not None else None


async def set_news_status(
    pool: asyncpg.Pool,
    news_id: int,
    status: NewsStatus,
    *,
    stage: str,
    message: str | None = None,
    level: str = "info",
    duplicate_of_id: int | None = None,
) -> None:
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "UPDATE news SET status = $2, duplicate_of_id = $3 WHERE id = $1",
            news_id, status.value, duplicate_of_id,
        )
        if message:
            await _log(conn, news_id, stage, level, message)


async def set_news_embedding(pool: asyncpg.Pool, news_id: int, embedding) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE news SET embedding = $2 WHERE id = $1", news_id, _vec(embedding)
        )


async def nearest_news(
    pool: asyncpg.Pool,
    embedding,
    *,
    exclude_id: int,
    cutoff: dt.datetime,
    limit: int,
) -> list[News]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_NEWS_COLUMNS} FROM news"
            " WHERE id != $1 AND embedding IS NOT NULL AND created_at >= $2"
            " ORDER BY embedding <=> $3 LIMIT $4",
            exclude_id, cutoff, _vec(embedding), limit,
        )
    return [_news(row) for row in rows]


async def log_stage(pool: asyncpg.Pool, news_id: int, stage: str, level: str, message: str) -> None:
    async with pool.acquire() as conn:
        await _log(conn, news_id, stage, level, message)


async def _log(conn, news_id: int, stage: str, level: str, message: str) -> None:
    await conn.execute(
        "INSERT INTO processing_log (news_id, stage, level, message) VALUES ($1, $2, $3, $4)",
        news_id, stage, level, message,
    )


async def processing_log_stages(pool: asyncpg.Pool, news_id: int) -> set[str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT stage FROM processing_log WHERE news_id = $1", news_id
        )
    return {row["stage"] for row in rows}


# --- posts ---


async def insert_post(pool: asyncpg.Pool, news_id: int, text: str, status: PostStatus) -> int:
    async with pool.acquire() as conn:
        post_id = await conn.fetchval(
            "INSERT INTO posts (news_id, text, status) VALUES ($1, $2, $3) RETURNING id",
            news_id, text, status.value,
        )
    return int(post_id)


async def get_post(pool: asyncpg.Pool, post_id: int) -> Post | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(f"SELECT {_POST_COLUMNS} FROM posts WHERE id = $1", post_id)
    return _post(row) if row else None


async def get_post_images(pool: asyncpg.Pool, post_id: int) -> list[PostImage]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, post_id, source_url, local_path, position FROM post_images"
            " WHERE post_id = $1 ORDER BY position",
            post_id,
        )
    return [PostImage(**dict(row)) for row in rows]


async def add_post_images(pool: asyncpg.Pool, post_id: int, source_urls: list[str]) -> None:
    async with pool.acquire() as conn, conn.transaction():
        for position, source_url in enumerate(source_urls):
            await conn.execute(
                "INSERT INTO post_images (post_id, source_url, local_path, position)"
                " VALUES ($1, $2, NULL, $3)",
                post_id, source_url, position,
            )


async def add_post_references(pool: asyncpg.Pool, post_id: int, referenced_ids: list[int]) -> None:
    if not referenced_ids:
        return
    async with pool.acquire() as conn, conn.transaction():
        await conn.executemany(
            "INSERT INTO post_references (post_id, referenced_post_id) VALUES ($1, $2)"
            " ON CONFLICT DO NOTHING",
            [(post_id, referenced_id) for referenced_id in referenced_ids],
        )


async def get_post_references(pool: asyncpg.Pool, post_id: int) -> list[int]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT referenced_post_id FROM post_references WHERE post_id = $1",
            post_id,
        )
    return [int(row["referenced_post_id"]) for row in rows]


async def update_post_text(pool: asyncpg.Pool, post_id: int, text: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute("UPDATE posts SET text = $2 WHERE id = $1", post_id, text)


async def set_post_tg_message(pool: asyncpg.Pool, post_id: int, tg_message_id: int, tg_url: str | None) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE posts SET tg_message_id = $2, tg_url = $3 WHERE id = $1",
            post_id, tg_message_id, tg_url,
        )


async def update_post_published(
    pool: asyncpg.Pool,
    post_id: int,
    *,
    tg_message_id: int,
    tg_url: str | None,
    embedding,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE posts SET status = $2, tg_message_id = $3, tg_url = $4, embedding = $5,"
            " published_at = now() WHERE id = $1",
            post_id, PostStatus.published.value, tg_message_id, tg_url, _vec(embedding),
        )


async def set_post_status(pool: asyncpg.Pool, post_id: int, status: PostStatus) -> None:
    async with pool.acquire() as conn:
        await conn.execute("UPDATE posts SET status = $2 WHERE id = $1", post_id, status.value)


async def delete_post(pool: asyncpg.Pool, post_id: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM posts WHERE id = $1", post_id)


async def delete_posts_for_news(
    pool: asyncpg.Pool,
    news_id: int,
    statuses: tuple[PostStatus, ...] = (PostStatus.draft, PostStatus.failed),
) -> int:
    """Delete a news's leftover posts (default: draft/failed); returns how many rows were removed."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "DELETE FROM posts WHERE news_id = $1 AND status = ANY($2) RETURNING id",
            news_id, [status.value for status in statuses],
        )
    return len(rows)


async def expire_old_drafts(pool: asyncpg.Pool, cutoff: dt.datetime) -> list[tuple[int, int]]:
    """Delete stale moderation drafts; returns (post_id, news_id) pairs."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "DELETE FROM posts WHERE status = $1 AND created_at < $2"
            " RETURNING id, news_id",
            PostStatus.draft.value, cutoff,
        )
    return [(int(row["id"]), int(row["news_id"])) for row in rows]


async def posts_by_status(pool: asyncpg.Pool, statuses: list[PostStatus]) -> list[Post]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_POST_COLUMNS} FROM posts WHERE status = ANY($1) ORDER BY created_at",
            [status.value for status in statuses],
        )
    return [_post(row) for row in rows]


async def nearest_published_posts(
    pool: asyncpg.Pool,
    embedding,
    *,
    cutoff: dt.datetime,
    limit: int,
) -> list[Post]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_POST_COLUMNS} FROM posts"
            " WHERE status = $1 AND embedding IS NOT NULL AND created_at >= $2"
            " ORDER BY embedding <=> $3 LIMIT $4",
            PostStatus.published.value, cutoff, _vec(embedding), limit,
        )
    return [_post(row) for row in rows]


async def count_post_images(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as conn:
        return int(await conn.fetchval("SELECT COUNT(*) FROM post_images"))


# --- recovery ---


async def reset_unfinished_news(pool: asyncpg.Pool) -> list[int]:
    """Delete unfinished posts and reset their news rows to pending for reprocessing.

    Unfinished = news not in a terminal state (published/duplicate/rejected/failed)
    and not awaiting moderation or queued for publication. Requeues pending news
    as well: their in-memory pipeline queue was lost on shutdown.
    Returns the ids of reset news rows (ascending).
    """
    in_flight = [
        NewsStatus.pending.value,
        NewsStatus.dedup.value,
        NewsStatus.photo_search.value,
        NewsStatus.writing.value,
    ]
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "DELETE FROM posts WHERE news_id IN (SELECT id FROM news WHERE status = ANY($1))",
            in_flight,
        )
        rows = await conn.fetch(
            "UPDATE news SET status = $2, duplicate_of_id = NULL"
            " WHERE status = ANY($1) RETURNING id",
            in_flight, NewsStatus.pending.value,
        )
    return [int(row["id"]) for row in rows]
