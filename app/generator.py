from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from app.config import Settings
from app.db.entities import Post
from app.providers.llm import LLMError, LLMProvider
from app.textutil import plain_text, sanitize_telegram_html

log = logging.getLogger("generator")

MAX_POST_LENGTH = 1000
MIN_POST_LENGTH = 200

DEFAULT_STYLE = (
    "Ты — админ новостного Telegram-канала. Пишешь посты живым языком: коротко, по делу, "
    "без канцелярита и кликбейта."
)

GENERATION_RULES = """

ТРЕБОВАНИЯ К ПОСТУ:
- Напиши пост в стиле канала (см. стайл-гайд), а не пересказ источника.
- Используй только факты из приведённой новости. Ничего не выдумывай и не добавляй детали, которых нет в тексте.
- Разметка — HTML Telegram: разрешены <b>, <i>, <u>, <s>, <a href="...">, <code>, <pre>. Заголовки и списки запрещены.
- Длина текста не более 1000 символов (лимит caption при фото).
- Если найденные прошлые посты канала посвящены развитию этой же истории — это продолжение темы: прямо укажи это («Как мы писали ранее…») и дай ссылку на прошлый пост (t.me-ссылка из списка), а его id включи в references_post_ids.
- Если это не продолжение — references_post_ids верни пустым списком.
"""

REFERENCES_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "references_post_ids": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["text", "references_post_ids"],
    "additionalProperties": False,
}


@dataclass(slots=True)
class PostDraft:
    text: str
    reference_ids: list[int]


class PostGenerator:
    def __init__(self, cfg: Settings, llm: LLMProvider, style_path) -> None:
        self._cfg = cfg
        self._llm = llm
        self._style_path = style_path

    async def generate(self, news, related: Sequence[Post]) -> PostDraft:
        style = self._load_style()
        system = style + GENERATION_RULES
        user = _build_user_prompt(news, related)
        data = await self._llm.complete_json(
            system=system,
            user=user,
            schema=REFERENCES_SCHEMA,
            schema_name="channel_post",
            temperature=self._cfg.llm.temperature,
        )
        text = sanitize_telegram_html(str(data.get("text") or ""))
        if not text:
            raise LLMError("generator returned empty post text")
        if len(text) > MAX_POST_LENGTH:
            text = await self._shorten(system, user, text)
        allowed_ids = {post.id for post in related}
        reference_ids = [int(pid) for pid in data.get("references_post_ids") or [] if int(pid) in allowed_ids]
        return PostDraft(text=text, reference_ids=reference_ids)

    async def _shorten(self, system: str, user: str, original_text: str) -> str:
        log.warning("generated post exceeds %d chars, asking llm to shorten", MAX_POST_LENGTH)
        try:
            data = await self._llm.complete_json(
                system=system,
                user=user + f"\n\nВНИМАНИЕ: предыдущий ответ был длиннее {MAX_POST_LENGTH} символов. "
                f"Сократи текст до {MAX_POST_LENGTH} символов или меньше, сохранив смысл и разметку.",
                schema=REFERENCES_SCHEMA,
                schema_name="channel_post",
                temperature=0.2,
            )
            text = sanitize_telegram_html(str(data.get("text") or ""))
            if MIN_POST_LENGTH <= len(text) <= MAX_POST_LENGTH:
                return text
        except LLMError:
            pass
        return plain_text(original_text)[:MAX_POST_LENGTH]

    def _load_style(self) -> str:
        try:
            content = self._style_path.read_text(encoding="utf-8")
        except OSError:
            log.warning("style file %s not readable, using built-in style", self._style_path)
            return DEFAULT_STYLE
        if not content.strip():
            return DEFAULT_STYLE
        return content


def _build_user_prompt(news, related: Sequence[Post]) -> str:
    lines = [
        "НОВОСТЬ:",
        f"Заголовок: {news.title}",
        f"Дата: {_fmt_date(news.published_at)}",
        f"Текст: {news.text[:6000]}",
    ]
    if related:
        lines.append("")
        lines.append("ПРОШЛЫЕ ПОСТЫ КАНАЛА, ПОХОЖИЕ ПО ТЕМЕ:")
        for post in related:
            lines.append(f"[id={post.id}] ({_fmt_date(post.created_at)}) {post.tg_url or '(ссылка недоступна)'}")
            lines.append(f"Текст поста: {post.text[:1500]}")
    return "\n".join(lines)


def _fmt_date(value: dt.datetime | None) -> str:
    if value is None:
        return "неизвестно"
    return value.strftime("%Y-%m-%d %H:%M")
