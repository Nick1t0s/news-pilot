from __future__ import annotations

import datetime as dt
import html
from zoneinfo import ZoneInfo

import asyncpg

from app.config import Settings
from app.db import repo

_COUNTER_TITLES = {
    repo.COUNTER_DUPLICATES: "🧹 Дубликатов",
    repo.COUNTER_SKIPPED_UNIMPORTANT: "✂️ Скипнуто (неважные)",
    repo.COUNTER_LIMIT_SKIPPED: "🚫 Скипнуто (дневной лимит)",
    repo.COUNTER_FAILED: "💀 Ошибок (failed)",
    repo.COUNTER_CLEARED: "🧾 Пропущено (clear_run)",
}


async def build_stats_text(
    pool: asyncpg.Pool,
    cfg: Settings,
    queued_count: int,
    drafts_count: int = 0,
    processing_queued: int = 0,
    processing_active: int = 0,
) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    day_ago = now - dt.timedelta(hours=24)
    week_ago = now - dt.timedelta(days=7)
    limits = cfg.limits
    start_of_day = None
    if limits.daily_posts > 0:
        tz = ZoneInfo(limits.timezone)
        start_of_day = dt.datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    async with pool.acquire() as conn:
        published = {
            "24ч": await _posts_count(conn, day_ago),
            "7д": await _posts_count(conn, week_ago),
            "всего": await _posts_count(conn, None),
        }
        published_today = await _posts_count(conn, start_of_day) if start_of_day is not None else None
        sources = await conn.fetch(
            "SELECT source, COUNT(*) AS total FROM posts WHERE published_at >= $1 GROUP BY source ORDER BY total DESC",
            day_ago,
        )
        last_post = await conn.fetchrow(
            "SELECT tg_url, tg_message_id, published_at FROM posts ORDER BY published_at DESC LIMIT 1",
        )
    counters = {c.key: c.value for c in await repo.counters_all(pool)}

    lines: list[str] = ["<b>📊 Статистика канала</b>", ""]
    lines.append("<b>24ч / 7д / всего</b>")
    lines.append(f"✅ Опубликовано: {published['24ч']} / {published['7д']} / {published['всего']}")
    for key, title in _COUNTER_TITLES.items():
        value = counters.get(key)
        if value:
            lines.append(f"{title}: {value}")
    lines.append("")
    lines.append("<b>Текущее состояние:</b>")
    lines.append(f"📥 Ожидают обработки: {processing_queued}")
    lines.append(f"⚙️ В работе: {processing_active}")
    if published_today is not None:
        remaining = max(0, limits.daily_posts - published_today)
        lines.append(f"🌅 Осталось постов на сегодня: {remaining} из {limits.daily_posts}")
    lines.append(f"🕓 Ждут модерации: {drafts_count}")
    lines.append(f"📤 В очереди публикации: {queued_count}")
    if last_post is not None:
        url = last_post["tg_url"] or f"message {last_post['tg_message_id']}"
        published_at = last_post["published_at"]
        lines.append("")
        lines.append(
            f"Последний пост: {html.escape(str(url))} ({published_at:%d.%m %H:%M})"
        )
    if sources:
        lines.append("")
        lines.append("<b>По источникам (24ч):</b>")
        for row in sources:
            lines.append(f"• {html.escape(str(row['source']))} — {int(row['total'])}")
    return "\n".join(lines)


async def _posts_count(conn: asyncpg.Connection, cutoff: dt.datetime | None) -> int:
    query = "SELECT COUNT(*) FROM posts"
    args: list = []
    if cutoff is not None:
        query += " WHERE published_at >= $1"
        args.append(cutoff)
    return int(await conn.fetchval(query, *args))
