from __future__ import annotations

import asyncio
import logging

import asyncpg

from app.db import repo
from app.db.entities import NewsStatus
from app.logging import set_news_id

log = logging.getLogger("pipeline")


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

    async def run_worker(self, worker_id: int = 0) -> None:
        log.info("worker started: id=%d", worker_id)
        while True:
            news_id = await self._queue.get()
            try:
                await self.process(news_id)
            finally:
                self._queue.task_done()

    async def process(self, news_id: int) -> None:
        set_news_id(news_id)
        log.info("processing started")
        try:
            verdict = await self._run_dedup(news_id)
            if verdict is not None:
                return
            photos = await self._run_photo(news_id)
            await self._run_writing_and_publish(news_id, photos)
        except Exception as exc:
            log.exception("pipeline stage failed")
            await self._fail(news_id, str(exc))
        finally:
            set_news_id(None)

    async def _run_dedup(self, news_id: int) -> bool | None:
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
        log.info("processing finished: result=%s", verdict.kind)
        return True

    async def _run_photo(self, news_id: int) -> list:
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
        log.info("photo stage done: found=%d", len(photos))
        return photos

    async def _run_writing_and_publish(self, news_id: int, photos: list) -> None:
        await repo.set_news_status(self._pool, news_id, NewsStatus.writing, stage="writing", message="stage started")
        news = await repo.get_news(self._pool, news_id)
        if news is None:
            raise LookupError(f"news {news_id} not found")
        related = await self._context.find(news)
        draft = await self._generator.generate(news, related)
        await self._publisher.submit(news, draft, photos)

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
