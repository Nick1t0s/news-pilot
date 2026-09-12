from __future__ import annotations

import asyncio
import logging

import asyncpg
import httpx

from app.config import Settings
from app.db import repo
from app.db.entities import NewsStatus
from app.fetcher import fetch_article
from app.rss.parse import parse_feed

log = logging.getLogger("poller")


class FeedPoller:
    def __init__(
        self,
        cfg: Settings,
        http: httpx.AsyncClient,
        pool: asyncpg.Pool,
        queue: asyncio.Queue[int],
    ) -> None:
        self._cfg = cfg
        self._http = http
        self._pool = pool
        self._queue = queue

    async def run_forever(self) -> None:
        try:
            if self._cfg.rss.clear_run:
                cleared = await self.poll_once(clear=True)
                if cleared:
                    log.info("clear run: %d preexisting feed items marked cleared", cleared)
        except Exception:
            log.exception("clear run poll failed")
        while True:
            try:
                added = await self.poll_once()
                if added:
                    log.info("poll done: %d new items", added)
            except Exception:
                log.exception("poll cycle failed")
            await asyncio.sleep(self._cfg.rss.poll_interval_seconds)

    async def poll_once(self, *, clear: bool = False) -> int:
        results = await asyncio.gather(
            *(self._poll_feed(feed, clear=clear) for feed in self._cfg.rss.feeds),
            return_exceptions=True,
        )
        added = 0
        for feed, result in zip(self._cfg.rss.feeds, results):
            if isinstance(result, BaseException):
                log.error("feed failed: source=%s error=%s", feed.name, result)
            else:
                added += result
        return added

    async def _poll_feed(self, feed, *, clear: bool = False) -> int:
        resp = await self._http.get(feed.url, timeout=self._cfg.fetcher.timeout_seconds)
        resp.raise_for_status()
        items = parse_feed(resp.content, feed.name)
        added = 0
        for item in items:
            if await self.ingest_item(item, clear=clear):
                added += 1
        return added

    async def ingest_item(self, item, *, clear: bool = False) -> bool:
        """Fetch full text and persist a new news row. Returns True if a new row was created."""
        if await repo.news_exists(self._pool, item.source, item.external_id):
            return False

        if clear:
            news_id = await repo.add_news(
                self._pool,
                source=item.source,
                external_id=item.external_id,
                title=item.title,
                text=item.summary,
                url=item.link,
                full_text_fetched=False,
                published_at=item.published_at,
                status=NewsStatus.cleared,
                log_stage_name="fetch",
                log_message="present in feed at startup (clear_run), skipped",
            )
            log.info("cleared: source=%s news_id=%d title=%r", item.source, news_id, item.title[:80])
            return True

        text, fetched = await self._load_text(item)
        if len(text) < self._cfg.fetcher.min_text_length:
            await repo.add_news(
                self._pool,
                source=item.source,
                external_id=item.external_id,
                title=item.title,
                text=text,
                url=item.link,
                full_text_fetched=fetched,
                published_at=item.published_at,
                status=NewsStatus.failed,
                log_stage_name="fetch",
                log_message=f"text too short ({len(text)} < {self._cfg.fetcher.min_text_length}), skipped",
            )
            log.warning("text too short, skipped: source=%s title=%r", item.source, item.title[:80])
            return True

        news_id = await repo.add_news(
            self._pool,
            source=item.source,
            external_id=item.external_id,
            title=item.title,
            text=text,
            url=item.link,
            full_text_fetched=fetched,
            published_at=item.published_at,
            status=NewsStatus.pending,
            log_stage_name="fetch",
            log_message="full text fetched" if fetched else "full text fetch failed, using rss summary",
        )
        self._queue.put_nowait(news_id)
        log.info("new item queued: source=%s news_id=%d title=%r", item.source, news_id, item.title[:80])
        return True

    async def _load_text(self, item) -> tuple[str, bool]:
        fetched_text = await fetch_article(
            self._http,
            item.link,
            timeout=self._cfg.fetcher.timeout_seconds,
            retries=self._cfg.fetcher.retries,
        )
        if fetched_text:
            return fetched_text, True
        return item.summary, False
