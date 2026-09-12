from __future__ import annotations

import asyncio
import logging
import time

import asyncpg

from app.db import repo
from app.db.entities import News, NewsStatus
from app.logging import set_news_id

log = logging.getLogger("pipeline")


def _short_title(news: News | None) -> str:
    return news.title[:80] if news else ""


def _duration_note(seconds: float) -> str:
    total = max(1, round(seconds))
    if total >= 60:
        return f"обработано за {total // 60} мин {total % 60} с"
    return f"обработано за {total} с"


class Pipeline:
    """News processing pipeline: dedup -> photo -> writing -> publish."""

    def __init__(
        self,
        cfg,
        pool: asyncpg.Pool,
        queue: asyncio.Queue[int],
        dedup,
        photo_agent,
        context_search,
        generator,
        publisher,
    ) -> None:
        self._cfg = cfg
        self._pool = pool
        self._queue = queue
        self._dedup = dedup
        self._photo = photo_agent
        self._context = context_search
        self._generator = generator
        self._publisher = publisher

    async def run(self) -> None:
        """Sequential pipeline loop: processes one news item at a time."""
        log.info("pipeline loop started")
        while True:
            news_id = await self._queue.get()
            try:
                await self.process(news_id)
            finally:
                self._queue.task_done()

    async def process(self, news_id: int) -> None:
        set_news_id(news_id)
        started = time.monotonic()
        news = await repo.get_news(self._pool, news_id)
        title = _short_title(news)
        log.info("processing started: title=%r", title)
        try:
            verdict = await self._run_dedup(news_id)
            if verdict is not None:
                log.info("processing finished: result=%s in %.1fs", verdict, time.monotonic() - started)
                return
            photos = await self._run_photo(news_id)
            await self._run_writing_and_publish(news_id, photos, time.monotonic() - started)
            log.info("processing finished: result=draft in %.1fs", time.monotonic() - started)
        except Exception as exc:
            log.exception("pipeline stage failed")
            await self._fail(news_id, str(exc))
        finally:
            set_news_id(None)

    async def _run_dedup(self, news_id: int) -> str | None:
        stage = time.monotonic()
        await repo.set_news_status(self._pool, news_id, NewsStatus.dedup, stage="dedup", message="stage started")
        verdict = await self._dedup.process(news_id)
        if verdict.kind == "unique":
            return None
        if verdict.kind == "duplicate":
            await repo.set_news_status(
                self._pool, news_id, NewsStatus.duplicate,
                stage="dedup",
                message=f"duplicate of {verdict.duplicate_of_id}: {verdict.reason}",
                duplicate_of_id=verdict.duplicate_of_id,
            )
        elif verdict.kind == "needs_review":
            await repo.set_news_status(
                self._pool, news_id, NewsStatus.needs_review,
                stage="dedup",
                message=f"needs review: {verdict.reason}",
                level="warn",
            )
        else:  # dropped
            await repo.set_news_status(
                self._pool, news_id, NewsStatus.failed,
                stage="dedup",
                message=f"dropped by dedup error policy: {verdict.reason}",
                level="error",
            )
        log.info("stage=dedup verdict=%s in %.1fs", verdict.kind, time.monotonic() - stage)
        return verdict.kind

    async def _run_photo(self, news_id: int) -> list:
        stage = time.monotonic()
        await repo.set_news_status(self._pool, news_id, NewsStatus.photo_search, stage="photo", message="stage started")
        news = await repo.get_news(self._pool, news_id)
        if news is None:
            raise LookupError(f"news {news_id} not found")
        try:
            photos = await self._photo.collect(news)
        except Exception as exc:  # noqa: BLE001
            log.warning("photo agent crashed, publishing without photos: %s", exc)
            photos = []
        await repo.log_stage(self._pool, news_id, "photo", "info", f"photo stage done: found={len(photos)}")
        log.info("stage=photo found=%d in %.1fs", len(photos), time.monotonic() - stage)
        return photos

    async def _run_writing_and_publish(self, news_id: int, photos: list, elapsed: float) -> None:
        stage = time.monotonic()
        await repo.set_news_status(self._pool, news_id, NewsStatus.writing, stage="writing", message="stage started")
        news = await repo.get_news(self._pool, news_id)
        if news is None:
            raise LookupError(f"news {news_id} not found")
        related = await self._context.find(news)
        draft = await self._generator.generate(news, related)
        await self._publisher.submit(news, draft, photos, note=_duration_note(elapsed))
        log.info("stage=writing done in %.1fs", time.monotonic() - stage)

    async def _fail(self, news_id: int, message: str) -> None:
        try:
            await repo.set_news_status(
                self._pool, news_id, NewsStatus.failed,
                stage="pipeline",
                message=f"unhandled error: {message}",
                level="error",
            )
        except Exception:
            log.exception("failed to persist error state for news %s", news_id)
