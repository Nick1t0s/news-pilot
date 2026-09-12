from __future__ import annotations

import datetime as dt
import html

import asyncpg

from app.config import Settings
from app.db.entities import NewsStatus, PostStatus


async def build_stats_text(pool: asyncpg.Pool, cfg: Settings, queued_count: int) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    day_ago = now - dt.timedelta(hours=24)
    week_ago = now - dt.timedelta(days=7)
    async with pool.acquire() as conn:
        received = {
            "24ч": await _news_count(conn, day_ago),
            "7д": await _news_count(conn, week_ago),
            "всего": await _news_count(conn, None),
        }
        published = {
            "24ч": await _post_status_count(conn, PostStatus.published, day_ago),
            "7д": await _post_status_count(conn, PostStatus.published, week_ago),
            "всего": await _post_status_count(conn, PostStatus.published, None),
        }
        duplicates = {
            "24ч": await _news_status_count(conn, NewsStatus.duplicate, day_ago),
            "7д": await _news_status_count(conn, NewsStatus.duplicate, week_ago),
            "всего": await _news_status_count(conn, NewsStatus.duplicate, None),
        }
        failed = {
            "24ч": await _news_status_count(conn, NewsStatus.failed, day_ago),
            "7д": await _news_status_count(conn, NewsStatus.failed, week_ago),
            "всего": await _news_status_count(conn, NewsStatus.failed, None),
        }
        needs_review = {
            "24ч": await _news_status_count(conn, NewsStatus.needs_review, day_ago),
            "7д": await _news_status_count(conn, NewsStatus.needs_review, week_ago),
            "всего": await _news_status_count(conn, NewsStatus.needs_review, None),
        }
        processing = await conn.fetchval(
            "SELECT COUNT(*) FROM news WHERE status = ANY($1)",
            [
                NewsStatus.pending.value,
                NewsStatus.dedup.value,
                NewsStatus.photo_search.value,
                NewsStatus.writing.value,
            ],
        )
        drafts = await conn.fetchval(
            "SELECT COUNT(*) FROM posts WHERE status = $1", PostStatus.draft.value
        )
        last_post = await conn.fetchrow(
            "SELECT tg_url, tg_message_id, created_at FROM posts"
            " WHERE status = $1 ORDER BY created_at DESC LIMIT 1",
            PostStatus.published.value,
        )
        sources = await conn.fetch(
            "SELECT source, COUNT(*) AS total FROM news WHERE created_at >= $1 GROUP BY source ORDER BY total DESC",
            day_ago,
        )

    lines: list[str] = ["<b>📊 Статистика канала</b>", ""]
    lines.append("<b>24ч / 7д / всего</b>")

    def _row(label: str, data: dict) -> str:
        return f"{label}: {data['24ч']} / {data['7д']} / {data['всего']}"

    lines.append(_row("📥 Получено новостей", received))
    lines.append(_row("✅ Опубликовано", published))
    lines.append(_row("🧹 Дубликатов", duplicates))
    lines.append(_row("💀 Ошибок (failed)", failed))
    lines.append(_row("⚠️ Требуют проверки", needs_review))
    lines.append("")
    lines.append("<b>Текущее состояние:</b>")
    lines.append(f"🔄 В обработке: {int(processing)}")
    lines.append(f"🕓 Ждут модерации: {int(drafts)}")
    lines.append(f"📤 В очереди публикации: {queued_count}")
    if last_post is not None:
        url = last_post["tg_url"] or f"message {last_post['tg_message_id']}"
        created_at = last_post["created_at"]
        lines.append("")
        lines.append(
            f"Последний пост: {html.escape(str(url))} ({created_at:%d.%m %H:%M})"
        )
    if sources:
        lines.append("")
        lines.append("<b>По источникам (24ч):</b>")
        for row in sources:
            lines.append(f"• {html.escape(str(row['source']))} — {int(row['total'])}")
    return "\n".join(lines)


async def _news_count(conn: asyncpg.Connection, cutoff: dt.datetime | None) -> int:
    query = "SELECT COUNT(*) FROM news"
    args: list = []
    if cutoff is not None:
        query += " WHERE created_at >= $1"
        args.append(cutoff)
    return int(await conn.fetchval(query, *args))


async def _news_status_count(
    conn: asyncpg.Connection, status: NewsStatus, cutoff: dt.datetime | None
) -> int:
    query = "SELECT COUNT(*) FROM news WHERE status = $1"
    args: list = [status.value]
    if cutoff is not None:
        query += " AND created_at >= $2"
        args.append(cutoff)
    return int(await conn.fetchval(query, *args))


async def _post_status_count(
    conn: asyncpg.Connection, status: PostStatus, cutoff: dt.datetime | None
) -> int:
    query = "SELECT COUNT(*) FROM posts WHERE status = $1"
    args: list = [status.value]
    if cutoff is not None:
        query += " AND created_at >= $2"
        args.append(cutoff)
    return int(await conn.fetchval(query, *args))
