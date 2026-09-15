from __future__ import annotations

import asyncio
import datetime as dt
import html
import itertools
import logging

import asyncpg

from app.bot.keyboards import moderation_keyboard
from app.config import Settings
from app.db import repo
from app.db.entities import DraftJob, PublishJob
from app.photo.downloader import download_image
from app.providers.embeddings import EmbeddingProvider
from app.textutil import plain_text

log = logging.getLogger("publish")

PhotoData = tuple[str, bytes]


class PublishService:
    """Publishes PublishJobs from an in-memory queue; drafts for moderation
    live in memory too (they die with the process)."""

    def __init__(self, cfg: Settings, pool: asyncpg.Pool, sender, embeddings: EmbeddingProvider, http) -> None:
        self._cfg = cfg
        self._pool = pool
        self._sender = sender
        self._embeddings = embeddings
        self._http = http
        self.queue: asyncio.Queue[PublishJob] = asyncio.Queue()
        self._drafts: dict[int, DraftJob] = {}
        self._draft_ids = itertools.count(1)
        self._photos: dict[int, list[PhotoData]] = {}  # draft_id -> photo bytes
        self._send_retries: dict[int, int] = {}

    async def submit(self, job: PublishJob) -> None:
        if self._cfg.publish.mode == "moderation":
            await self.send_draft(job)
        else:
            self.queue.put_nowait(job)

    async def approve_draft(self, draft_id: int, *, drop_photos: bool = False) -> DraftJob:
        """Publish a moderation draft (or a queued job by its draft slot)."""
        draft = self._drafts.pop(draft_id, None)
        if draft is None:
            raise LookupError(f"draft {draft_id} not found (lost on restart?)")
        await self._publish(draft.job, draft_id, drop_photos=drop_photos)
        return draft

    async def _publish(self, job: PublishJob, key: int, *, drop_photos: bool = False) -> None:
        photos = [] if drop_photos else await self._photos_for_send(key, job)
        text = await self._text_with_source(job)
        message_id, tg_url = await self._sender.send_to_channel(
            text, photos, reply_to=job.reply_to_message_id,
        )
        embedding = await self._safe_embed(job.text)
        post_id = await repo.insert_published_post(
            self._pool,
            source=job.source,
            text=job.text,
            tg_message_id=message_id,
            tg_url=tg_url,
            embedding=embedding,
        )
        if self._cfg.publish.mode == "auto" and self._cfg.publish.notify_admin:
            await self._notify_admin_published(job, tg_url or message_id)
        self._send_retries.pop(key, None)
        self._photos.pop(key, None)
        log.info(
            "published: post_id=%d message_id=%d photos=%d source=%s",
            post_id, message_id, len(photos), job.source,
        )

    async def _photos_for_send(self, key: int, job: PublishJob) -> list[PhotoData]:
        photos = [(p.source_url, p.data) for p in job.photos if p.data]
        if photos:
            return photos
        urls = [p.source_url for p in job.photos if p.source_url]
        if not urls:
            return []
        photos = await self._refetch_photos(urls)
        if photos:
            self._photos[key] = photos
        return photos

    async def _refetch_photos(self, urls: list[str]) -> list[PhotoData]:
        photos: list[PhotoData] = []
        for url in urls:
            data = await download_image(self._http, url, timeout=20.0, retries=2)
            if data is not None:
                photos.append((url, data))
            else:
                log.warning("photo refetch failed, publishing without it: url=%s", url[:200])
        return photos

    async def _text_with_source(self, job: PublishJob) -> str:
        if not self._cfg.publish.append_source or not job.source_url:
            return job.text
        return f'{job.text}\n\n🔗 <a href="{html.escape(job.source_url, quote=True)}">Источник</a>'

    async def _notify_admin_published(self, job: PublishJob, link: int | str) -> None:
        try:
            await self._sender.send_admin_text(
                f"✅ Опубликовано: {link}\n📰 Источник: {html.escape(job.source, quote=False)}"
            )
        except Exception:
            log.exception("failed to notify admin about published post: source=%s", job.source)

    async def reject_draft(self, draft_id: int, reason: str) -> None:
        draft = self._drafts.pop(draft_id, None)
        if draft is None:
            return
        self._photos.pop(draft_id, None)
        self._send_retries.pop(draft_id, None)
        try:
            await repo.counter_increment(self._pool, repo.COUNTER_REJECTED)
        except Exception:
            log.exception("failed to increment counter %s", repo.COUNTER_REJECTED)
        log.info("rejected: draft_id=%d reason=%s", draft_id, reason)

    async def apply_edit(self, draft_id: int, new_text: str) -> None:
        draft = self._drafts.get(draft_id)
        if draft is not None:
            draft.job.text = new_text

    async def run_queue_worker(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                await self._publish(job, id(job))
            except Exception as exc:
                log.exception("publish failed: source=%s", job.source)
                await self._handle_publish_failure(job, id(job), str(exc))
            finally:
                self.queue.task_done()

    async def _handle_publish_failure(self, job: PublishJob, key: int, message: str) -> None:
        retries = self._send_retries.get(key, 0) + 1
        self._send_retries[key] = retries
        if retries >= 3:
            self._send_retries.pop(key, None)
            try:
                await repo.counter_increment(self._pool, repo.COUNTER_FAILED)
            except Exception:
                log.exception("failed to increment counter %s", repo.COUNTER_FAILED)
            return
        self.queue.put_nowait(job)
        await asyncio.sleep(5.0 * retries)

    async def run_moderation_watch(self) -> None:
        while True:
            await self.expire_stale_drafts()
            await asyncio.sleep(60.0)

    async def expire_stale_drafts(self) -> int:
        """Reject drafts older than moderation_timeout_hours. Returns how many."""
        timeout = dt.timedelta(hours=self._cfg.publish.moderation_timeout_hours)
        cutoff = dt.datetime.now(dt.timezone.utc) - timeout
        expired = [draft_id for draft_id, draft in self._drafts.items() if draft.created_at < cutoff]
        for draft_id in expired:
            self._drafts.pop(draft_id, None)
            self._photos.pop(draft_id, None)
            await repo.counter_increment(self._pool, repo.COUNTER_REJECTED)
            log.info("draft rejected by timeout: draft_id=%d", draft_id)
            try:
                await self._sender.send_admin_text(
                    f"⏰ Черновик поста #{draft_id} отклонён по таймауту модерации."
                )
            except Exception:
                log.exception("failed to notify admin about timeout")
        return len(expired)

    async def send_draft(self, job: PublishJob) -> None:
        draft_id = next(self._draft_ids)
        photos = [(p.source_url, p.data) for p in job.photos if p.data]
        self._photos[draft_id] = photos
        text = job.text if not job.admin_note else f"{job.text}\n\n<i>{job.admin_note}</i>"
        keyboard = moderation_keyboard(draft_id, has_photos=bool(photos))
        message = await self._sender.send_moderation_draft(text, photos, keyboard)
        draft = DraftJob(
            id=draft_id,
            job=job,
            created_at=dt.datetime.now(dt.timezone.utc),
            admin_chat_id=message.chat.id,
            admin_message_id=message.message_id,
        )
        self._drafts[draft_id] = draft
        log.info("draft sent to admin: draft_id=%d photos=%d text_len=%d", draft_id, len(photos), len(text))

    def get_draft(self, draft_id: int) -> DraftJob | None:
        return self._drafts.get(draft_id)

    def pop_admin_message(self, draft_id: int) -> tuple[int | None, int] | None:
        draft = self._drafts.get(draft_id)
        if draft is None:
            return None
        ref = (draft.admin_chat_id, draft.admin_message_id)
        draft.admin_chat_id = None
        draft.admin_message_id = None
        return ref

    def queued_count(self) -> int:
        return self.queue.qsize()

    def drafts_count(self) -> int:
        return len(self._drafts)

    async def _safe_embed(self, text: str) -> list[float] | None:
        try:
            return await self._embeddings.embed(plain_text(text)[: self._cfg.embeddings.max_chars])
        except Exception as exc:  # noqa: BLE001
            log.warning("post embedding failed: %s", exc)
            return None
