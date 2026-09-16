from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

import asyncpg

from app.config import Settings
from app.db import repo
from app.db.entities import FeedItem, PublishedPost
from app.providers.embeddings import EmbeddingProvider
from app.providers.llm import LLMError, LLMProvider
from app.textutil import cosine_similarity

log = logging.getLogger("dedup")

DEDUP_SYSTEM = (
    "Ты — ассистент для дедупликации новостей. Тебе дают новую новость и список кандидатов — "
    "ранее опубликованных постов канала. Определи, описывает ли новая новость то же самое событие, "
    "что и один из кандидатов. Перефразировка, другой стиль подачи, частичное обновление при той же "
    "сути — это дубликат. Разные события на одну тему — не дубликат.\n"
    'Ответь строго в JSON: {"is_duplicate": bool, "reason": str}, '
    "где reason — краткое объяснение решения."
)

DEDUP_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "is_duplicate": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["is_duplicate", "reason"],
    "additionalProperties": False,
}

_CANDIDATE_TEXT_LIMIT = 1500


@dataclass(slots=True)
class DedupVerdict:
    kind: str  # unique | duplicate | dropped
    reason: str = ""


class DedupService:
    def __init__(self, cfg: Settings, pool: asyncpg.Pool, llm: LLMProvider, embeddings: EmbeddingProvider) -> None:
        self._cfg = cfg
        self._pool = pool
        self._llm = llm
        self._embeddings = embeddings

    async def process(self, item: FeedItem) -> tuple[DedupVerdict, list[float]]:
        """Gate a feed item: only the duplicate check. Returns the verdict and
        the item's embedding (reused later for context search, so it is
        computed only once)."""
        embedding = await self._embeddings.embed(f"{item.title}\n{item.text[: self._cfg.embeddings.max_chars]}")
        candidates = await self._nearest_candidates(embedding)
        verdict_data = await self._ask_llm(item, candidates)
        if verdict_data is None:
            return await self._on_error(), embedding
        return self._verdict_from_data(verdict_data), embedding

    def _verdict_from_data(self, verdict_data: dict) -> DedupVerdict:
        reason = str(verdict_data.get("reason") or "")
        if bool(verdict_data.get("is_duplicate")):
            return DedupVerdict("duplicate", reason=reason)
        return DedupVerdict("unique", reason=reason)

    async def _nearest_candidates(self, embedding: list[float]) -> list[tuple[PublishedPost, float]]:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=self._cfg.dedup.window_days)
        rows = await repo.nearest_posts(
            self._pool,
            embedding,
            cutoff=cutoff,
            limit=self._cfg.dedup.top_k,
        )
        min_similarity = self._cfg.dedup.min_similarity
        candidates: list[tuple[PublishedPost, float]] = []
        for row in rows:
            similarity = cosine_similarity(embedding, row.embedding)
            if similarity >= min_similarity:
                candidates.append((row, similarity))
        return candidates

    async def _ask_llm(self, item: FeedItem, candidates: list[tuple[PublishedPost, float]]) -> dict | None:
        user = _build_prompt(item, [c for c, _ in candidates])
        try:
            return await self._llm.complete_json(
                system=DEDUP_SYSTEM, user=user, schema=DEDUP_SCHEMA, schema_name="dedup_verdict", temperature=0.0
            )
        except LLMError as exc:
            log.error("dedup llm failed: title=%r error=%s", item.title[:80], exc)
            return None

    async def _on_error(self) -> DedupVerdict:
        policy = self._cfg.dedup.on_error
        message = f"llm error after retries, dedup.on_error={policy}"
        if policy == "drop":
            return DedupVerdict("dropped", reason=message)
        return DedupVerdict("unique", reason=message + " (pass policy)")


def _build_prompt(item: FeedItem, candidates: list[PublishedPost]) -> str:
    lines = [
        "НОВАЯ НОВОСТЬ:",
        f"Заголовок: {item.title}",
        f"Дата: {_fmt_date(item.published_at)}",
        f"Текст: {item.text[:_CANDIDATE_TEXT_LIMIT]}",
        "",
        "КАНДИДАТЫ (ранее опубликованные посты):",
    ]
    if candidates:
        for candidate in candidates:
            lines.append(f"[id={candidate.id}] ({_fmt_date(candidate.published_at)})")
            lines.append(f"Текст поста: {candidate.text[:_CANDIDATE_TEXT_LIMIT]}")
    else:
        lines.append("(кандидатов нет — новость уникальна)")
    return "\n".join(lines)


def _fmt_date(value: dt.datetime | None) -> str:
    if value is None:
        return "неизвестно"
    return value.strftime("%Y-%m-%d %H:%M")
