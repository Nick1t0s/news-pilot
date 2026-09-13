from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.config import Settings
from app.db.entities import Post
from app.providers.llm import LLMError, LLMProvider
from app.textutil import sanitize_telegram_html, truncate_html

log = logging.getLogger("generator")

MAX_POST_LENGTH = 1000
MIN_POST_LENGTH = 200

_TME_URL_RE = re.compile(r"https?://(?:t\.me|telegram\.me)/[^\s)]*", re.IGNORECASE)
_EMPTY_PARENS_RE = re.compile(r"\(\s*\)")

DEFAULT_STYLE = (
    "Ты — админ новостного Telegram-канала. Пишешь посты живым языком: коротко, по делу, "
    "без канцелярита и кликбейта."
)

GENERATION_RULES = """

ТРЕБОВАНИЯ К ПОСТУ:
- Напиши пост в стиле канала (см. стайл-гайд), а не пересказ источника.
- Используй только факты из приведённой новости. Ничего не выдумывай и не добавляй детали, которых нет в тексте.
- Разметка — HTML Telegram: разрешены <b>, <i>, <u>, <s>, <code>, <pre>. Заголовки, списки и любые ссылки запрещены.
- Длина текста не более 1000 символов (лимит caption при фото).
- Если найденные прошлые посты канала посвящены развитию этой же истории — это продолжение темы: включи id прошлого
  поста в references_post_ids (бот отправит пост ответом на прошлый). Ссылку в тексте НЕ вставляй.
- В посте-продолжении рассказывай только о новых фактах (то, чего не было в прошлом посте). Не повторяй уже
  известные детали, цифры и статусы из прошлого поста.
- ЗАПРЕЩЕНЫ обороты вроде «Как мы писали ранее», «Ранее мы сообщали», «Мы уже рассказывали» — просто продолжай
  изложение новыми фактами.
- Никаких URL и ссылок в тексте поста — запрещено.
- Не упоминай названия СМИ и формулировки вроде «по данным СМИ», «как сообщает …», «по информации …» — если новость
  можно передать без этого, передавай без этого.
- Финал поста — без призывов писать комментарии («пишите в комментариях», «как вы считаете» и т.п.): комментарии
  отключены. Вопрос читателям допустим только без упоминания комментариев.
- Нейтральная подача: факты, заявления, решения, события — да; собственные негативные, обвинительные или
  провокационные интерпретации в отношении Путина, России, российского правительства, госорганов, законов, СВО
  и действий российских властей — запрещены. Отделяй факт новости от эмоциональной оценки.
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
        text = sanitize_telegram_html(_strip_tme_urls(str(data.get("text") or "")))
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
            text = sanitize_telegram_html(_strip_tme_urls(str(data.get("text") or "")))
            if MIN_POST_LENGTH <= len(text) <= MAX_POST_LENGTH:
                return text
        except LLMError:
            pass
        return truncate_html(original_text, MAX_POST_LENGTH)

    def _load_style(self) -> str:
        try:
            content = self._style_path.read_text(encoding="utf-8")
        except OSError:
            log.warning("style file %s not readable, using built-in style", self._style_path)
            return DEFAULT_STYLE
        if not content.strip():
            return DEFAULT_STYLE
        return content


def _strip_tme_urls(text: str) -> str:
    cleaned = _TME_URL_RE.sub("", text)
    cleaned = _EMPTY_PARENS_RE.sub("", cleaned)
    return re.sub(r"[ \t]{2,}", " ", cleaned)


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
            lines.append(f"[id={post.id}] ({_fmt_date(post.created_at)})")
            lines.append(f"Текст поста: {post.text[:1500]}")
    return "\n".join(lines)


def _fmt_date(value: dt.datetime | None) -> str:
    if value is None:
        return "неизвестно"
    return value.strftime("%Y-%m-%d %H:%M")
