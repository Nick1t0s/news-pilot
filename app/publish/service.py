from __future__ import annotations

import asyncio
import datetime as dt
import html
import logging

import asyncpg

from app.bot.keyboards import moderation_keyboard
from app.config import Settings
from app.db import repo
from app.db.entities import News, NewsStatus, Post, PostStatus
from app.photo.downloader import download_image
from app.providers.embeddings import EmbeddingProvider
from app.textutil import plain_text

log = logging.getLogger("publish")

PhotoData = tuple[str, bytes]


class PublishService:
    """Creates post drafts, publishes via queue worker, handles moderation."""

    def __init__(self, cfg: Settings, pool: asyncpg.Pool, sender, embeddings: EmbeddingProvider, http) -> None:
        self._cfg = cfg
        self._pool = pool
        self._sender = sender
        self._embeddings = embeddings
        self._http = http
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self._admin_msgs: dict[int, tuple[int | None, int]] = {}
        self._post_retries: dict[int, int] = {}
        self._photos: dict[int, list[PhotoData]] = {}

    async def submit(self, news: News, draft, photos: list, note: str | None = None) -> None:
        post_id = await repo.insert_post(self._pool, news.id, draft.text, self._initial_status())
        photo_data: list[PhotoData] = [
            (photo.source_url, photo.data) for photo in photos if photo.data
        ]
        self._photos[post_id] = photo_data
        await repo.add_post_images(self._pool, post_id, [url for url, _ in photo_data])
        await repo.add_post_references(self._pool, post_id, list(getattr(draft, "reference_ids", []) or []))
        await repo.log_stage(
            self._pool, news.id, "publish", "info",
            f"post draft created id={post_id}, mode={self._cfg.publish.mode}",
        )
        await self._dispatch(post_id, news, note)

    def _initial_status(self) -> PostStatus:
        if self._cfg.publish.mode == "moderation":
            return PostStatus.draft
        return PostStatus.queued

    async def _dispatch(self, post_id: int, news: News, note: str | None) -> None:
        if self._cfg.publish.mode == "moderation":
            await self.send_draft(post_id, note=note)
            status, message = NewsStatus.moderation, "draft sent to admin for moderation"
        else:
            self.queue.put_nowait(post_id)
            status, message = NewsStatus.queued, "queued for publication"
        await repo.set_news_status(self._pool, news.id, status, stage="publish", message=message)

    async def approve(self, post_id: int, *, drop_photos: bool = False) -> Post:
        post = await repo.get_post(self._pool, post_id)
        if post is None:
            raise LookupError(f"post {post_id} not found")
        if post.tg_message_id is not None:
            message_id = post.tg_message_id
            tg_url = post.tg_url
            photos_count = 0
        else:
            photos = [] if drop_photos else await self._photos_for_send(post_id)
            photos_count = len(photos)
            reply_to = await self._resolve_reply_target(post_id)
            text = await self._text_with_source(post)
            message_id, tg_url = await self._sender.send_to_channel(text, photos, reply_to=reply_to)
            await repo.set_post_tg_message(self._pool, post_id, message_id, tg_url)
        embedding = await self._safe_embed(post.text)
        await repo.update_post_published(
            self._pool, post_id, tg_message_id=message_id, tg_url=tg_url, embedding=embedding,
        )
        note = " without photos" if drop_photos or (photos_count == 0 and not drop_photos) else ""
        await repo.set_news_status(
            self._pool, post.news_id, NewsStatus.published,
            stage="publish", message=f"published{note} {tg_url or message_id}",
        )
        if self._cfg.publish.mode == "auto" and self._cfg.publish.notify_admin:
            await self._notify_admin_published(post_id, tg_url or message_id)
        self._post_retries.pop(post_id, None)
        self._photos.pop(post_id, None)
        log.info("published: post_id=%d message_id=%d photos=%d", post_id, message_id, photos_count)
        return await repo.get_post(self._pool, post_id)

    async def _photos_for_send(self, post_id: int) -> list[PhotoData]:
        """In-memory photo bytes; refetch by source_url after restart if lost."""
        photos = self._photos.get(post_id)
        if photos is not None:
            return photos
        images = await repo.get_post_images(self._pool, post_id)
        urls = [image.source_url for image in images if image.source_url]
        if not urls:
            return []
        photos = await self._refetch_photos(urls)
        if photos:
            self._photos[post_id] = photos
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

    async def _text_with_source(self, post: Post) -> str:
        if not self._cfg.publish.append_source:
            return post.text
        news = await repo.get_news(self._pool, post.news_id)
        if news is None or not news.url:
            return post.text
        return f'{post.text}\n\n🔗 <a href="{html.escape(news.url, quote=True)}">Источник</a>'

    async def _notify_admin_published(self, post_id: int, link: int | str) -> None:
        try:
            post = await repo.get_post(self._pool, post_id)
            news = await repo.get_news(self._pool, post.news_id) if post else None
            if news is None:
                return
            now = dt.datetime.now(dt.timezone.utc)
            stamp = news.published_at or news.created_at
            age = f"\n⏱ {_humanize_age(now - stamp)} назад" if stamp else ""
            source = f'<a href="{html.escape(news.url, quote=True)}">{html.escape(news.source)}</a>' if news.url else news.source
            await self._sender.send_admin_text(
                f"✅ Опубликовано: {link}\n📰 Источник: {source}{age}"
            )
        except Exception:
            log.exception("failed to notify admin about published post: post_id=%s", post_id)

    async def _resolve_reply_target(self, post_id: int) -> int | None:
        referenced_ids = await repo.get_post_references(self._pool, post_id)
        if not referenced_ids:
            return None
        epoch = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        posts = []
        for pid in referenced_ids:
            post = await repo.get_post(self._pool, pid)
            if post is not None and post.tg_message_id is not None:
                posts.append(post)
        if not posts:
            return None
        freshest = max(posts, key=lambda post: (post.published_at or epoch, post.id))
        return freshest.tg_message_id

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
        self._photos.pop(post_id, None)
        log.info("rejected: post_id=%d reason=%s", post_id, reason)

    async def apply_edit(self, post_id: int, new_text: str) -> None:
        await repo.update_post_text(self._pool, post_id, new_text)

    async def run_queue_worker(self) -> None:
        while True:
            post_id = await self.queue.get()
            try:
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
                    self._photos.pop(post_id, None)
                    log.info("draft rejected by timeout: post_id=%s", post_id)
                    try:
                        await self._sender.send_admin_text(f"⏰ Черновик поста #{post_id} отклонён по таймауту модерации.")
                    except Exception:
                        log.exception("failed to notify admin about timeout")
            except Exception:
                log.exception("moderation watch cycle failed")
            await asyncio.sleep(60.0)

    async def restore(self) -> None:
        posts = await repo.posts_by_status(self._pool, [PostStatus.queued, PostStatus.draft])
        queued = [post for post in posts if post.status == PostStatus.queued]
        drafts = [post for post in posts if post.status == PostStatus.draft]
        for post in queued:
            self.queue.put_nowait(post.id)
        for post in drafts:
            try:
                if self._cfg.publish.mode == "auto":
                    await self.approve(post.id)
                else:
                    await self.send_draft(post.id)
            except Exception:
                log.exception("failed to re-send draft after restart: post_id=%s", post.id)
        if queued or drafts:
            log.info("restored after restart: queued=%d drafts=%d", len(queued), len(drafts))

    async def send_draft(self, post_id: int, note: str | None = None) -> None:
        post = await repo.get_post(self._pool, post_id)
        if post is None:
            return
        photos = await self._photos_for_send(post_id)

        text = post.text if note is None else f"{post.text}\n\n<i>{note}</i>"
        keyboard = moderation_keyboard(post_id, has_photos=bool(photos))
        message = await self._sender.send_moderation_draft(text, photos, keyboard)
        self._admin_msgs[post_id] = (message.chat.id, message.message_id)
        log.info("draft sent to admin: post_id=%d photos=%d text_len=%d", post_id, len(photos), len(text))

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


def _plural(value: int, one: str, few: str, many: str) -> str:
    if value % 10 == 1 and value % 100 != 11:
        return one
    if value % 10 in {2, 3, 4} and value % 100 not in {12, 13, 14}:
        return few
    return many


def _humanize_age(delta: dt.timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return "меньше минуты"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} {_plural(minutes, 'минуту', 'минуты', 'минут')}"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} {_plural(hours, 'час', 'часа', 'часов')}"
    days = hours // 24
    return f"{days} {_plural(days, 'день', 'дня', 'дней')}"


def _seconds_until_quiet_end(now: dt.datetime, window: tuple[dt.time, dt.time], tz) -> float:
    start, end = window
    end_dt = dt.datetime.combine(now.date(), end, tzinfo=tz)
    if end <= start and end_dt <= now:
        end_dt += dt.timedelta(days=1)
    if end_dt <= now:
        end_dt += dt.timedelta(days=1)
    return max(1.0, (end_dt - now).total_seconds())
