from __future__ import annotations

import datetime as dt
import logging

import asyncpg

from app.db import repo
from app.db.entities import FeedItem
from app.providers.llm import LLMError, LLMProvider
from app.textutil import humanize_age

log = logging.getLogger("selection")

_BATCH_TEXT_LIMIT = 600

SELECT_SYSTEM = (
    "Ты — редактор новостного Telegram-канала. Тебе дают батч уникальных новостей (дубликаты уже "
    "отфильтрованы) и контекст публикации канала. Выбери ровно одну новость — самую значимую и "
    "интересную читателям: важность, свежесть, масштаб события. Рутинные и второстепенные новости "
    "выбирай только если значимых в батче нет. Учитывай контекст публикации: свежесть последнего "
    "поста и равномерность постов в течение дня.\n"
    'Ответь строго в JSON: {"selected_index": int | null, "reason": str}, '
    "где selected_index — номер выбранной новости из списка (1..N) или null, если ни одна не "
    "достойна поста, reason — краткое объяснение выбора."
)

SELECT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "selected_index": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "reason": {"type": "string"},
    },
    "required": ["selected_index", "reason"],
    "additionalProperties": False,
}


class SelectionError(RuntimeError):
    """The selector returned an unusable answer; the batch is re-queued."""


class PublishContext:
    """Builds the publication-context block for the selection prompt (time, last post, cadence)."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def build(self) -> str:
        now = dt.datetime.now(dt.timezone.utc)
        last_at = await repo.last_published_post_at(self._pool)
        lines = [
            "КОНТЕКСТ ПУБЛИКАЦИИ:",
            f"Сейчас: {now.strftime('%Y-%m-%d %H:%M')} (UTC).",
        ]
        if last_at is None:
            lines.append("Последний пост: неизвестно (постов ещё не публиковалось).")
        else:
            age = humanize_age(now - last_at)
            lines.append(
                f"Последний пост опубликован в {last_at.astimezone(dt.timezone.utc).strftime('%Y-%m-%d %H:%M')} "
                f"({age} назад)."
            )
        lines.append(
            "Публикуй посты равномерно в течение дня и береги канал от шума: если последний пост "
            "вышел только что (несколько минут назад) — выбирай только действительно значимую "
            "новость; если постов давно не было — можно выбрать и умеренно значимую."
        )
        return "\n".join(lines)


class NewsSelector:
    """Picks the single most newsworthy item from a batch of unique news."""

    def __init__(self, pool: asyncpg.Pool, llm: LLMProvider) -> None:
        self._pool = pool
        self._llm = llm
        self._publish_context = PublishContext(pool)

    async def select(self, batch: list[FeedItem]) -> FeedItem | None:
        """One LLM call for the whole batch. Returns the chosen item or None
        (nothing worth publishing). Raises SelectionError on an unusable answer."""
        if not batch:
            return None
        publish_context = await self._publish_context.build()
        user = _build_prompt(batch, publish_context)
        try:
            data = await self._llm.complete_json(
                system=SELECT_SYSTEM,
                user=user,
                schema=SELECT_SCHEMA,
                schema_name="news_selection",
                temperature=0.0,
            )
        except LLMError as exc:
            raise SelectionError(f"selection llm failed: {exc}") from exc
        raw = data.get("selected_index")
        if raw is None:
            return None
        try:
            index = int(raw) - 1
        except (TypeError, ValueError) as exc:
            raise SelectionError(f"invalid selected_index: {raw!r}") from exc
        if not 0 <= index < len(batch):
            raise SelectionError(f"selected_index out of range: {raw!r} (batch size {len(batch)})")
        return batch[index]


def _build_prompt(batch: list[FeedItem], publish_context: str) -> str:
    lines = [
        publish_context,
        "",
        "БАТЧ УНИКАЛЬНЫХ НОВОСТЕЙ:",
    ]
    for index, item in enumerate(batch, start=1):
        lines.append(f"[{index}] ({_fmt_date(item.published_at)}) {item.title}")
        lines.append(f"Текст: {item.text[:_BATCH_TEXT_LIMIT]}")
    lines.append("")
    lines.append(
        f"Выбери одну новость (selected_index от 1 до {len(batch)}) или верни selected_index=null, "
        "если ни одна не достойна поста."
    )
    return "\n".join(lines)


def _fmt_date(value: dt.datetime | None) -> str:
    if value is None:
        return "неизвестно"
    return value.strftime("%Y-%m-%d %H:%M")
