from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from zoneinfo import ZoneInfo

import asyncpg

from app.db import repo
from app.db.entities import News, NewsStatus
from app.logging import set_news_id
from app.providers.embeddings import EmbeddingError
from app.providers.llm import LLMError

log = logging.getLogger("pipeline")


def _short_title(news: News | None) -> str:
    return news.title[:80] if news else ""


def _duration_note(seconds: float) -> str:
    total = max(1, round(seconds))
    if total >= 60:
        return f"обработано за {total // 60} мин {total % 60} с"
    return f"обработано за {total} с"


class Pipeline:
    """News processing pipeline: dedup -> photo -> writing -> publish.

    Each news item is processed in its own asyncio task; parallelism is capped
    by `pipeline.concurrency` (semaphore), so a slow item never blocks others.
    """

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
        self._semaphore = asyncio.Semaphore(max(1, cfg.pipeline.concurrency))
        self._tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        """Concurrent loop: spawns a task per news item, bounded by the semaphore."""
        log.info(
            "pipeline loop started (concurrency=%d)",
            max(1, self._cfg.pipeline.concurrency),
        )
        try:
            while True:
                news_id = await self._queue.get()
                task = asyncio.create_task(
                    self._process_guarded(news_id), name=f"pipeline-news-{news_id}"
                )
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        finally:
            # run() only ends on cancellation (shutdown): stop in-flight items too
            await self._drain_tasks()

    async def _process_guarded(self, news_id: int) -> None:
        try:
            async with self._semaphore:
                await self.process(news_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("pipeline task crashed: news_id=%d", news_id)
            await self._fail(news_id, "unhandled pipeline task crash")
        finally:
            self._queue.task_done()

    async def _drain_tasks(self) -> None:
        if not self._tasks:
            return
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def process(self, news_id: int) -> None:
        set_news_id(news_id)
        started = time.monotonic()
        max_retries = self._cfg.pipeline.retries
        for attempt in range(1, max_retries + 2):
            news = await repo.get_news(self._pool, news_id)
            if news is not None and news.status in (NewsStatus.queued, NewsStatus.moderation, NewsStatus.published):
                # the draft was already dispatched to the publish path; it owns the outcome now
                log.info("processing skipped: news %s already dispatched (status=%s)", news_id, news.status.value)
                return
            if attempt > 1:
                log.info("processing retry (attempt %d/%d): title=%r", attempt, max_retries + 1, _short_title(news))
            try:
                verdict = await self._run_dedup(news_id)
                if verdict is not None:
                    log.info("processing finished: result=%s in %.1fs", verdict, time.monotonic() - started)
                    return
                photos = await self._run_photo(news_id)
                await self._run_writing_and_publish(news_id, photos, time.monotonic() - started)
                log.info("processing finished: result=draft in %.1fs", time.monotonic() - started)
                return
            except Exception as exc:
                if isinstance(exc, (LLMError, EmbeddingError)):
                    log.error("pipeline stage failed: %s", exc)
                else:
                    log.exception("pipeline stage failed")
                if isinstance(exc, LookupError):
                    # news row is gone, retrying is pointless
                    await self._fail(news_id, str(exc))
                    return
                if attempt > max_retries:
                    message = f"{type(exc).__name__}: {exc}"
                    if attempt > 1:
                        message = f"failed after {attempt} attempts: {message}"
                    await self._fail(news_id, message)
                    return
                await self._prepare_retry(news_id, attempt, max_retries, exc)

    async def _prepare_retry(self, news_id: int, attempt: int, max_retries: int, exc: Exception) -> None:
        """Drop leftover drafts from the failed attempt and log the retry decision."""
        message = f"{type(exc).__name__}: {exc}"
        try:
            await repo.delete_posts_for_news(self._pool, news_id)
            await repo.log_stage(self._pool, news_id, "pipeline", "warn", f"retry {attempt}/{max_retries}: {message}")
        except Exception:
            log.exception("failed to persist retry state for news %s", news_id)
        log.warning(
            "retrying immediately (attempt %d/%d): news_id=%d error=%s",
            attempt + 1, max_retries + 1, news_id, message,
        )

    async def _run_dedup(self, news_id: int) -> str | None:
        stage = time.monotonic()
        if await self._daily_limit_reached():
            await repo.set_news_status(
                self._pool, news_id, NewsStatus.skipped,
                stage="dedup", message="daily post limit reached",
            )
            log.info("stage=dedup skipped: daily post limit reached in %.1fs", time.monotonic() - stage)
            return "skipped"
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
        elif verdict.kind == "skipped":
            await repo.set_news_status(
                self._pool, news_id, NewsStatus.skipped,
                stage="dedup",
                message=f"skipped by importance gate: {verdict.reason}",
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

    async def _daily_limit_reached(self) -> bool:
        daily_posts = self._cfg.limits.daily_posts
        if daily_posts <= 0:
            return False
        tz = ZoneInfo(self._cfg.limits.timezone)
        now = dt.datetime.now(tz)
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        published = await repo.count_published_posts_since(self._pool, since)
        if published >= daily_posts:
            log.info("daily limit reached: %d/%d posts published today (%s)", published, daily_posts, self._cfg.limits.timezone)
            return True
        return False

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
