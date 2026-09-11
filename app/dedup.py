from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

import asyncpg

from app.config import Settings
from app.db import repo
from app.db.entities import News
from app.providers.embeddings import EmbeddingProvider
from app.providers.llm import LLMError, LLMProvider
from app.textutil import cosine_similarity

log = logging.getLogger("dedup")

DEDUP_SYSTEM = (
    "Ты — ассистент для дедупликации новостей. Тебе дают новую новость и список кандидатов — "
    "ранее увиденных новостей. Определи, описывает ли новая новость то же самое событие, что и "
    "один из кандидатов. Перефразировка, другой стиль подачи, частичное обновление при той же "
    "сути — это дубликат. Разные события на одну тему — не дубликат.\n"
    'Ответь строго в JSON: {"is_duplicate": bool, "reason": str, "duplicate_of_id": int | null}, '
    "где duplicate_of_id — id новости-оригинала из кандидатов (null, если дубликата нет)."
)

DEDUP_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "is_duplicate": {"type": "boolean"},
        "reason": {"type": "string"},
        "duplicate_of_id": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
    },
    "required": ["is_duplicate", "reason", "duplicate_of_id"],
    "additionalProperties": False,
}

_CANDIDATE_TEXT_LIMIT = 1500


@dataclass(slots=True)
class DedupVerdict:
    kind: str  # unique | duplicate | needs_review | dropped
    duplicate_of_id: int | None = None
    reason: str = ""


class DedupService:
    def __init__(self, cfg: Settings, pool: asyncpg.Pool, llm: LLMProvider, embeddings: EmbeddingProvider) -> None:
        self._cfg = cfg
        self._pool = pool
        self._llm = llm
        self._embeddings = embeddings

    async def process(self, news_id: int) -> DedupVerdict:
        news = await repo.get_news(self._pool, news_id)
        if news is None:
            raise LookupError(f"news {news_id} not found")

        embedding = await self._embeddings.embed(f"{news.title}\n{news.text[: self._cfg.embeddings.max_chars]}")
        await repo.set_news_embedding(self._pool, news_id, embedding)
        candidates = await self._nearest_candidates(news, embedding)
        if not candidates:
            await repo.log_stage(self._pool, news_id, "dedup", "info", "no candidates, news is unique")
            return DedupVerdict("unique")

        verdict_data = await self._ask_llm(news, candidates)
        if verdict_data is None:
            return await self._on_error(news_id)
        is_duplicate = bool(verdict_data.get("is_duplicate"))
        reason = str(verdict_data.get("reason") or "")
        if not is_duplicate:
            await repo.log_stage(self._pool, news_id, "dedup", "info", f"unique: {reason}")
            return DedupVerdict("unique", reason=reason)
        duplicate_of = self._resolve_duplicate_id(verdict_data, candidates)
        await repo.log_stage(self._pool, news_id, "dedup", "info", f"duplicate of {duplicate_of}: {reason}")
        return DedupVerdict("duplicate", duplicate_of_id=duplicate_of, reason=reason)

    async def _nearest_candidates(self, news: News, embedding: list[float]) -> list[tuple[News, float]]:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=self._cfg.dedup.window_days)
        rows = await repo.nearest_news(
            self._pool,
            embedding,
            exclude_id=news.id,
            cutoff=cutoff,
            limit=self._cfg.dedup.top_k,
        )
        min_similarity = self._cfg.dedup.min_similarity
        candidates: list[tuple[News, float]] = []
        for row in rows:
            similarity = cosine_similarity(embedding, row.embedding)
            if similarity >= min_similarity:
                candidates.append((row, similarity))
        return candidates

    async def _ask_llm(self, news: News, candidates: list[tuple[News, float]]) -> dict | None:
        user = _build_prompt(news, [c for c, _ in candidates])
        try:
            return await self._llm.complete_json(
                system=DEDUP_SYSTEM, user=user, schema=DEDUP_SCHEMA, schema_name="dedup_verdict", temperature=0.0
            )
        except LLMError as exc:
            log.error("dedup llm failed: news_id=%d error=%s", news.id, exc)
            return None

    async def _on_error(self, news_id: int) -> DedupVerdict:
        policy = self._cfg.dedup.on_error
        message = f"llm error after retries, dedup.on_error={policy}"
        if policy == "review":
            await repo.log_stage(self._pool, news_id, "dedup", "error", message + " -> needs_review")
            return DedupVerdict("needs_review", reason="dedup llm error")
        if policy == "pass":
            await repo.log_stage(self._pool, news_id, "dedup", "warn", message + " -> treated as unique")
            return DedupVerdict("unique", reason="dedup llm error (pass policy)")
        await repo.log_stage(self._pool, news_id, "dedup", "error", message + " -> dropped")
        return DedupVerdict("dropped", reason="dedup llm error (drop policy)")

    def _resolve_duplicate_id(self, verdict_data: dict, candidates: list[tuple[News, float]]) -> int | None:
        candidate_ids = [c.id for c, _ in candidates]
        raw = verdict_data.get("duplicate_of_id")
        try:
            value = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return candidate_ids[0] if candidate_ids else None
        if value in candidate_ids:
            return value
        return candidate_ids[0] if candidate_ids else None


def _build_prompt(news: News, candidates: list[News]) -> str:
    lines = [
        "НОВАЯ НОВОСТЬ:",
        f"Заголовок: {news.title}",
        f"Дата: {_fmt_date(news.published_at)}",
        f"Текст: {news.text[:_CANDIDATE_TEXT_LIMIT]}",
        "",
        "КАНДИДАТЫ (ранее увиденные новости):",
    ]
    for candidate in candidates:
        lines.append(f"[id={candidate.id}] ({_fmt_date(candidate.published_at or candidate.created_at)})")
        lines.append(f"Заголовок: {candidate.title}")
        lines.append(f"Текст: {candidate.text[:_CANDIDATE_TEXT_LIMIT]}")
    return "\n".join(lines)


def _fmt_date(value: dt.datetime | None) -> str:
    if value is None:
        return "неизвестно"
    return value.strftime("%Y-%m-%d %H:%M")
