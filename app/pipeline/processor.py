from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time

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
    """Two-stage news processing.

    Stage 1 (the only parallel stage): dedup workers — one asyncio task per
    item, capped by `pipeline.concurrency`. Each item gets an embedding and
    one LLM duplicate check; unique items (with the embedding attached) go
    into the second, in-memory queue.

    Stage 2 (serial): every `pipeline.batch_interval_seconds` that queue is
    drained to zero; the whole batch goes into one LLM call that picks exactly
    one item (the losers are discarded for good), and the winner goes through
    photo -> context search -> generation -> publisher queue, one at a time.

    Items live only in memory: there are no statuses in the DB, outcomes are
    either a PublishJob in the publisher queue or a counter increment.
    """

    def __init__(
        self,
        cfg,
        pool: asyncpg.Pool,
        queue: asyncio.Queue[FeedItem],
        dedup,
        selection,
        photo_agent,
        context_search,
        generator,
        publisher,
    ) -> None:
        self._cfg = cfg
        self._pool = pool
        self._queue = queue
        self._unique_queue: asyncio.Queue[FeedItem] = asyncio.Queue()
        self._dedup = dedup
        self._selection = selection
        self._photo = photo_agent
        self._context = context_search
        self._generator = generator
        self._publisher = publisher
        self._semaphore = asyncio.Semaphore(max(1, cfg.pipeline.concurrency))
        self._tasks: set[asyncio.Task] = set()

    def queued_count(self) -> int:
        """Items waiting in the dedup queue (not yet picked up)."""
        return self._queue.qsize()

    def dedup_active(self) -> int:
        """Items currently in the dedup stage (including semaphore waiters)."""
        return len(self._tasks)

    def selection_queued(self) -> int:
        """Unique items waiting for the next batch selection."""
        return self._unique_queue.qsize()

    async def run(self) -> None:
        """Dedup loop: spawns a task per item, bounded by the semaphore."""
        log.info(
            "dedup loop started (concurrency=%d)",
            max(1, self._cfg.pipeline.concurrency),
        )
        try:
            while True:
                item = await self._queue.get()
                task = asyncio.create_task(
                    self._dedup_guarded(item), name=f"pipeline-dedup-{item.external_id[:40]}"
                )
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        finally:
            # run() only ends on cancellation (shutdown): stop in-flight items too
            await self._drain_tasks()

    async def run_selection(self) -> None:
        """Serial loop: once per interval, drain the unique queue, pick one, write it."""
        interval = max(1, self._cfg.pipeline.batch_interval_seconds)
        log.info("selection loop started (interval=%ds)", interval)
        while True:
            await asyncio.sleep(interval)
            try:
                await self.select_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("selection cycle failed")

    async def dedup_one(self, item: FeedItem) -> None:
        """Run one item through the dedup gate: duplicate -> counter, unique -> selection queue."""
        verdict, embedding = await self._dedup.process(item)
        item.embedding = embedding or None
        log.info("stage=dedup verdict=%s (%s)", verdict.kind, verdict.reason)
        if verdict.kind != "unique":
            await self._apply_verdict(verdict)
            return
        self._unique_queue.put_nowait(item)

    async def select_once(self) -> FeedItem | None:
        """One selection cycle: drain the unique queue, let the model pick one,
        write the winner. Returns the chosen item (or None)."""
        batch = self._drain_unique()
        if not batch:
            return None
        started = time.monotonic()
        try:
            chosen = await self._selection.select(batch)
        except Exception as exc:  # noqa: BLE001
            # unusable verdict: give the batch back, retry on the next tick
            log.error("selection failed, re-queuing batch (%d items): %s", len(batch), exc)
            for item in batch:
                self._unique_queue.put_nowait(item)
            return None
        for item in batch:
            if item is not chosen:
                await self._increment(repo.COUNTER_NOT_SELECTED)
        if chosen is None:
            log.info(
                "selection: nothing worth publishing (batch=%d in %.1fs)",
                len(batch), time.monotonic() - started,
            )
            return None
        log.info(
            "selection done: batch=%d discarded=%d title=%r in %.1fs",
            len(batch), len(batch) - 1, _short_title(chosen), time.monotonic() - started,
        )
        await self.write_one(chosen)
        return chosen

    async def write_one(self, item: FeedItem) -> None:
        """Serial writing stage: photo -> context -> generation -> publish queue,
        with `pipeline.retries` attempts before the item is marked failed."""
        started = time.monotonic()
        max_retries = self._cfg.pipeline.retries
        for attempt in range(1, max_retries + 2):
            if attempt > 1:
                log.info("writing retry (attempt %d/%d): title=%r", attempt, max_retries + 1, _short_title(item))
            try:
                photos = await self._run_photo(item)
                await self._run_writing_and_publish(item, photos, time.monotonic() - started)
                log.info("writing finished: published in %.1fs", time.monotonic() - started)
                return
            except Exception as exc:
                if isinstance(exc, (LLMError, EmbeddingError)):
                    log.error("writing stage failed: %s", exc)
                else:
                    log.exception("writing stage failed")
                if attempt > max_retries:
                    await self._fail(item, f"{type(exc).__name__}: {exc}")
                    return
                log.warning(
                    "retrying immediately (attempt %d/%d): title=%r error=%s",
                    attempt + 1, max_retries + 1, _short_title(item), f"{type(exc).__name__}: {exc}",
                )

    def _drain_unique(self) -> list[FeedItem]:
        items: list[FeedItem] = []
        while True:
            try:
                items.append(self._unique_queue.get_nowait())
            except asyncio.QueueEmpty:
                return items

    async def _apply_verdict(self, verdict: DedupVerdict) -> None:
        if verdict.kind == "duplicate":
            counter = repo.COUNTER_DUPLICATES
        else:  # dropped (dedup.on_error=drop)
            counter = repo.COUNTER_FAILED
        await self._increment(counter)

    async def _increment(self, key: str) -> None:
        await repo.counter_increment(self._pool, key)

    async def _run_photo(self, item: FeedItem) -> list:
        stage = time.monotonic()
        try:
            photos = await self._photo.collect(item)
        except Exception as exc:  # noqa: BLE001
            log.warning("photo agent crashed, publishing without photos: %s", exc)
            photos = []
        log.info("stage=photo found=%d in %.1fs", len(photos), time.monotonic() - stage)
        return photos

    async def _run_writing_and_publish(self, item: FeedItem, photos: list, elapsed: float) -> None:
        stage = time.monotonic()
        related = await self._context.find(item, item.embedding)
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

    async def _dedup_guarded(self, item: FeedItem) -> None:
        try:
            async with self._semaphore:
                await self.dedup_one(item)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("dedup task crashed: title=%r", item.title[:80])
            await self._fail(item, "unhandled dedup task crash")
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
