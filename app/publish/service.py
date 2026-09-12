from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections import deque
from zoneinfo import ZoneInfo

import asyncpg

from app.bot.keyboards import moderation_keyboard
from app.config import Settings
from app.db import repo
from app.db.entities import News, NewsStatus, Post, PostStatus
from app.providers.embeddings import EmbeddingProvider
from app.textutil import plain_text

log = logging.getLogger("publish")


def parse_quiet_hours(value: str | None) -> tuple[dt.time, dt.time] | None:
    if not value:
        return None
    start_raw, end_raw = value.split("-")

    def _time(part: str) -> dt.time:
        hour, minute = part.strip().split(":")
        return dt.time(int(hour), int(minute))

    return _time(start_raw), _time(end_raw)


def in_quiet_hours(now: dt.datetime, window: tuple[dt.time, dt.time]) -> bool:
    start, end = window
    current = now.time()
    if start <= end:
        return start <= current < end
    return current >= start or current < end


class PublishService:
    """Creates post drafts, applies rate limit/quiet hours, handles moderation."""

    def __init__(self, cfg: Settings, pool: asyncpg.Pool, sender, embeddings: EmbeddingProvider) -> None:
        self._cfg = cfg
        self._pool = pool
        self._sender = sender
        self._embeddings = embeddings
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self._publish_times: deque[dt.datetime] = deque()
        self._admin_msgs: dict[int, tuple[int | None, int]] = {}
        self._post_retries: dict[int, int] = {}

    async def submit(self, news: News, draft, photos: list) -> None:
        post_id = await repo.insert_post(self._pool, news.id, draft.text, self._initial_status())
        await repo.add_post_images(
            self._pool, post_id,
            [(photo.source_url, photo.local_path) for photo in photos],
        )
        await repo.add_post_references(self._pool, post_id, list(getattr(draft, "reference_ids", []) or []))
        await repo.log_stage(
            self._pool, news.id, "publish", "info",
            f"post draft created id={post_id}, mode={self._cfg.publish.mode}",
        )
        await self._dispatch(post_id, news)

    def _initial_status(self) -> PostStatus:
        if self._cfg.publish.mode == "moderation":
            return PostStatus.draft
        return PostStatus.queued

    async def _dispatch(self, post_id: int, news: News) -> None:
        if self._cfg.publish.mode == "moderation":
            await self.send_draft(post_id)
            message = "draft sent to admin for moderation"
        else:
            self.queue.put_nowait(post_id)
            message = "queued for publication"
        await repo.set_news_status(self._pool, news.id, NewsStatus.moderation, stage="publish", message=message)

    async def approve(self, post_id: int, *, drop_photos: bool = False) -> Post:
        post = await repo.get_post(self._pool, post_id)
        if post is None:
            raise LookupError(f"post {post_id} not found")
        images = await repo.get_post_images(self._pool, post_id)
        local_paths = [] if drop_photos else [image.local_path for image in images if image.local_path]
        message_id, tg_url = await self._sender.send_to_channel(post.text, local_paths)
        embedding = await self._safe_embed(post.text)
        await repo.update_post_published(
            self._pool, post_id, tg_message_id=message_id, tg_url=tg_url, embedding=embedding,
        )
        note = " without photos" if drop_photos else ""
        await repo.set_news_status(
            self._pool, post.news_id, NewsStatus.published,
            stage="publish", message=f"published{note} {tg_url or message_id}",
        )
        self._post_retries.pop(post_id, None)
        log.info("published: post_id=%d message_id=%d photos=%d", post_id, message_id, len(local_paths))
        return await repo.get_post(self._pool, post_id)

    async def reject(self, post_id: int, reason: str) -> None:
        post = await repo.get_post(self._pool, post_id)
        if post is None:
            return
        await repo.delete_post(self._pool, post_id)
        await repo.set_news_status(
            self._pool, post.news_id, NewsStatus.rejected,
            stage="publish", message=f"rejected: {reason}", level="warn",
        )
        self._admin_msgs.pop(post_id, None)
        self._post_retries.pop(post_id, None)
        log.info("rejected: post_id=%d reason=%s", post_id, reason)

    async def apply_edit(self, post_id: int, new_text: str) -> None:
        await repo.update_post_text(self._pool, post_id, new_text)

    async def run_queue_worker(self) -> None:
        while True:
            post_id = await self.queue.get()
            try:
                await self._wait_slot()
                await self.approve(post_id)
            except Exception as exc:
                log.exception("publish failed: post_id=%s", post_id)
                await self._handle_publish_failure(post_id, str(exc))
            finally:
                self.queue.task_done()

    async def _handle_publish_failure(self, post_id: int, message: str) -> None:
        retries = self._post_retries.get(post_id, 0) + 1
        self._post_retries[post_id] = retries
        if retries >= 3:
            self._post_retries.pop(post_id, None)
            post = await repo.get_post(self._pool, post_id)
            if post is not None:
                await repo.set_post_status(self._pool, post_id, PostStatus.failed)
                await repo.set_news_status(
                    self._pool, post.news_id, NewsStatus.failed,
                    stage="publish", message=f"publish failed: {message}", level="error",
                )
            return
        self.queue.put_nowait(post_id)
        await asyncio.sleep(5.0 * retries)

    async def _wait_slot(self) -> None:
        window = parse_quiet_hours(self._cfg.publish.quiet_hours)
        tz = ZoneInfo(self._cfg.publish.timezone)
        while True:
            now = dt.datetime.now(tz)
            if window is not None and in_quiet_hours(now, window):
                sleep_for = _seconds_until_quiet_end(now, window, tz)
                log.info("quiet hours active, sleeping %.0fs", sleep_for)
                await asyncio.sleep(sleep_for)
                continue
            cutoff = now.astimezone(dt.timezone.utc) - dt.timedelta(hours=1)
            while self._publish_times and self._publish_times[0] < cutoff:
                self._publish_times.popleft()
            if len(self._publish_times) < self._cfg.publish.max_per_hour:
                self._publish_times.append(now.astimezone(dt.timezone.utc))
                return
            oldest = self._publish_times[0]
            sleep_for = max(1.0, (oldest + dt.timedelta(hours=1) - now.astimezone(dt.timezone.utc)).total_seconds())
            log.info("rate limit reached (%d/hour), sleeping %.0fs", self._cfg.publish.max_per_hour, sleep_for)
            await asyncio.sleep(sleep_for)

    async def run_moderation_watch(self) -> None:
        timeout = dt.timedelta(hours=self._cfg.publish.moderation_timeout_hours)
        while True:
            try:
                cutoff = dt.datetime.now(dt.timezone.utc) - timeout
                expired = await repo.expire_old_drafts(self._pool, cutoff)
                for post_id, news_id in expired:
                    await repo.set_news_status(
                        self._pool, news_id, NewsStatus.rejected,
                        stage="publish", message="moderation timeout", level="warn",
                    )
                    self._admin_msgs.pop(post_id, None)
                    self._post_retries.pop(post_id, None)
                    log.info("draft rejected by timeout: post_id=%s", post_id)
                    try:
                        await self._sender.send_admin_text(f"⏰ Черновик поста #{post_id} отклонён по таймауту модерации.")
                    except Exception:
                        log.exception("failed to notify admin about timeout")
            except Exception:
                log.exception("moderation watch cycle failed")
            await asyncio.sleep(60.0)

    async def restore(self) -> None:
        await self._restore_rate_limit()
        posts = await repo.posts_by_status(self._pool, [PostStatus.queued, PostStatus.draft])
        queued = [post for post in posts if post.status == PostStatus.queued]
        drafts = [post for post in posts if post.status == PostStatus.draft]
        for post in queued:
            self.queue.put_nowait(post.id)
        for post in drafts:
            try:
                await self.send_draft(post.id)
            except Exception:
                log.exception("failed to re-send draft after restart: post_id=%s", post.id)
        if queued or drafts:
            log.info("restored after restart: queued=%d drafts=%d", len(queued), len(drafts))

    async def _restore_rate_limit(self) -> None:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
        self._publish_times = deque(
            sorted(await repo.recent_publish_times(self._pool, since))
        )
        if self._publish_times:
            log.info("rate limit window restored: %d posts in the last hour", len(self._publish_times))

    async def send_draft(self, post_id: int) -> None:
        post = await repo.get_post(self._pool, post_id)
        if post is None:
            return
        images = await repo.get_post_images(self._pool, post_id)
        local_paths = [image.local_path for image in images if image.local_path]

        keyboard = moderation_keyboard(post_id, has_photos=bool(local_paths))
        message = await self._sender.send_moderation_draft(post.text, local_paths, keyboard)
        self._admin_msgs[post_id] = (message.chat.id, message.message_id)
        log.info("draft sent to admin: post_id=%d photos=%d text_len=%d", post_id, len(local_paths), len(post.text))

    async def edit_draft_admin_message(self, post_id: int, new_text: str) -> None:
        ref = self._admin_msgs.get(post_id)
        if ref is None:
            return
        chat_id, message_id = ref
        try:
            await self._sender.edit_message(chat_id, message_id, new_text)
        except Exception:
            log.exception("failed to edit admin draft message: post_id=%s", post_id)

    def pop_admin_message(self, post_id: int) -> tuple[int | None, int] | None:
        return self._admin_msgs.pop(post_id, None)

    def queued_count(self) -> int:
        return self.queue.qsize()

    async def _safe_embed(self, text: str) -> list[float] | None:
        try:
            return await self._embeddings.embed(plain_text(text)[: self._cfg.embeddings.max_chars])
        except Exception as exc:  # noqa: BLE001
            log.warning("post embedding failed: %s", exc)
            return None


def _seconds_until_quiet_end(now: dt.datetime, window: tuple[dt.time, dt.time], tz) -> float:
    start, end = window
    end_dt = dt.datetime.combine(now.date(), end, tzinfo=tz)
    if end <= start and end_dt <= now:
        end_dt += dt.timedelta(days=1)
    if end_dt <= now:
        end_dt += dt.timedelta(days=1)
    return max(1.0, (end_dt - now).total_seconds())
