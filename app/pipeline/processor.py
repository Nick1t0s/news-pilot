from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from zoneinfo import ZoneInfo

import asyncpg

from app.db import repo
from app.db.entities import FeedItem, PublishJob
from app.dedup import DedupVerdict
from app.providers.embeddings import EmbeddingError
from app.providers.llm import LLMError

log = logging.getLogger("pipeline")


def _short_title(item: FeedItem | None) -> str:
    return item.title[:80] if item else ""


def _duration_note(seconds: float) -> str:
    total = max(1, round(seconds))
    if total >= 60:
        return f"обработано за {total // 60} мин {total % 60} с"
    return f"обработано за {total} с"


class Pipeline:
    """News processing: dedup/importance gate -> photo -> writing -> publish queue.

    Each item is processed in its own asyncio task; parallelism is capped
    by `pipeline.concurrency` (semaphore), so a slow item never blocks others.
    Items live only in memory: there are no statuses in the DB, outcomes are
    either a PublishJob in the publisher queue or a counter increment.
    """

    def __init__(
        self,
        cfg,
        pool: asyncpg.Pool,
        queue: asyncio.Queue[FeedItem],
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

    def queued_count(self) -> int:
        """Items waiting in the queue (not yet picked up)."""
        return self._queue.qsize()

    def in_flight_count(self) -> int:
        """Items picked up for processing (including those waiting for a semaphore slot)."""
        return len(self._tasks)

    async def run(self) -> None:
        """Concurrent loop: spawns a task per item, bounded by the semaphore."""
        log.info(
            "pipeline loop started (concurrency=%d)",
            max(1, self._cfg.pipeline.concurrency),
        )
        try:
            while True:
                item = await self._queue.get()
                task = asyncio.create_task(
                    self._process_guarded(item), name=f"pipeline-item-{item.external_id[:40]}"
                )
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        finally:
            # run() only ends on cancellation (shutdown): stop in-flight items too
            await self._drain_tasks()

    async def _process_guarded(self, item: FeedItem) -> None:
        try:
            async with self._semaphore:
                await self.process(item)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("pipeline task crashed: title=%r", item.title[:80])
            await self._fail(item, "unhandled pipeline task crash")
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

    async def process(self, item: FeedItem) -> None:
        started = time.monotonic()
        max_retries = self._cfg.pipeline.retries
        for attempt in range(1, max_retries + 2):
            if attempt > 1:
                log.info("processing retry (attempt %d/%d): title=%r", attempt, max_retries + 1, _short_title(item))
            try:
                verdict, embedding = await self._run_gate(item)
                if verdict.kind != "unique":
                    await self._apply_verdict(verdict)
                    log.info(
                        "processing finished: result=%s in %.1fs", verdict.kind, time.monotonic() - started
                    )
                    return
                photos = await self._run_photo(item)
                await self._run_writing_and_publish(item, embedding, photos, time.monotonic() - started)
                log.info("processing finished: result=published in %.1fs", time.monotonic() - started)
                return
            except Exception as exc:
                if isinstance(exc, (LLMError, EmbeddingError)):
                    log.error("pipeline stage failed: %s", exc)
                else:
                    log.exception("pipeline stage failed")
                if attempt > max_retries:
                    await self._fail(item, f"{type(exc).__name__}: {exc}")
                    return
                log.warning(
                    "retrying immediately (attempt %d/%d): title=%r error=%s",
                    attempt + 1, max_retries + 1, _short_title(item), f"{type(exc).__name__}: {exc}",
                )

    async def _apply_verdict(self, verdict: DedupVerdict) -> None:
        if verdict.kind == "duplicate":
            counter = repo.COUNTER_DUPLICATES
        elif verdict.kind == "skipped":
            counter = repo.COUNTER_SKIPPED_UNIMPORTANT
        elif verdict.kind == "needs_review":
            counter = None
        else:  # dropped
            counter = repo.COUNTER_FAILED
        if counter is not None:
            await self._increment(counter)

    async def _run_gate(self, item: FeedItem) -> tuple[DedupVerdict, list[float]]:
        if await self._daily_limit_reached():
            await self._increment(repo.COUNTER_LIMIT_SKIPPED)
            log.info("stage=gate skipped: daily post limit reached")
            return DedupVerdict("skipped", reason="daily post limit reached"), []
        verdict, embedding = await self._dedup.process(item)
        log.info("stage=gate verdict=%s (%s)", verdict.kind, verdict.reason)
        return verdict, embedding

    async def _increment(self, key: str) -> None:
        await repo.counter_increment(self._pool, key)

    async def _daily_limit_reached(self) -> bool:
        daily_posts = self._cfg.limits.daily_posts
        if daily_posts <= 0:
            return False
        tz = ZoneInfo(self._cfg.limits.timezone)
        now = dt.datetime.now(tz)
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        published = await repo.count_published_posts_since(self._pool, since)
        if published >= daily_posts:
            log.info(
                "daily limit reached: %d/%d posts published today (%s)",
                published, daily_posts, self._cfg.limits.timezone,
            )
            return True
        return False

    async def _run_photo(self, item: FeedItem) -> list:
        stage = time.monotonic()
        try:
            photos = await self._photo.collect(item)
        except Exception as exc:  # noqa: BLE001
            log.warning("photo agent crashed, publishing without photos: %s", exc)
            photos = []
        log.info("stage=photo found=%d in %.1fs", len(photos), time.monotonic() - stage)
        return photos

    async def _run_writing_and_publish(
        self, item: FeedItem, embedding: list[float], photos: list, elapsed: float
    ) -> None:
        stage = time.monotonic()
        related = await self._context.find(item, embedding)
        draft = await self._generator.generate(item, related)
        reply_to = self._resolve_reply_target(related, draft.reference_ids)
        job = PublishJob(
            source=item.source,
            text=draft.text,
            source_url=item.url,
            photos=list(photos),
            reply_to_message_id=reply_to,
            admin_note=_duration_note(elapsed),
        )
        await self._publisher.submit(job)
        log.info("stage=writing done in %.1fs", time.monotonic() - stage)

    def _resolve_reply_target(self, related, reference_ids: list[int]) -> int | None:
        """The freshest referenced post's telegram message id, if it is published."""
        by_id = {post.id: post for post in related}
        candidates = [by_id[pid] for pid in reference_ids if pid in by_id]
        if not candidates:
            return None
        epoch = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        freshest = max(
            candidates,
            key=lambda post: (post.published_at or epoch, post.id),
        )
        return freshest.tg_message_id

    async def _fail(self, item: FeedItem, message: str) -> None:
        try:
            await self._increment(repo.COUNTER_FAILED)
        except Exception:
            log.exception("failed to increment counter %s", repo.COUNTER_FAILED)
        log.error("item failed: title=%r error=%s", _short_title(item), message)
