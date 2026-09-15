from __future__ import annotations

import asyncio
import logging

import httpx

from app.config import Settings
from app.db import repo
from app.db.entities import FeedItem
from app.fetcher import fetch_article
from app.rss.parse import NewsItem, parse_feed

log = logging.getLogger("poller")

_SEEN_SET_MAX = 50_000


class FeedPoller:
    """Polls RSS feeds and puts fetched FeedItems into the processing queue.

    De-duplication of already-seen feed entries is an in-memory set of
    (source, external_id); it dies with the process. On restart clear_run
    re-marks everything currently in the feeds, and the vector dedup
    absorbs whatever slips through.
    """

    def __init__(
        self,
        cfg: Settings,
        http: httpx.AsyncClient,
        counters_pool,
        queue: asyncio.Queue[FeedItem],
    ) -> None:
        self._cfg = cfg
        self._http = http
        self._pool = counters_pool
        self._queue = queue
        self._seen: set[tuple[str, str]] = set()

    async def run_forever(self) -> None:
        try:
            if self._cfg.rss.clear_run:
                cleared = await self.poll_once(clear=True)
                if cleared:
                    log.info("clear run: %d preexisting feed items skipped", cleared)
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
        results = await asyncio.gather(
            *(self.ingest_item(item, clear=clear) for item in items),
            return_exceptions=True,
        )
        added = 0
        for item, result in zip(items, results):
            if isinstance(result, BaseException):
                log.error(
                    "ingest failed: source=%s external_id=%s error=%s",
                    item.source, item.external_id, result,
                )
            elif result:
                added += 1
        return added

    async def ingest_item(self, item: NewsItem, *, clear: bool = False) -> bool:
        """Fetch full text and enqueue the item. Returns True if it was new."""
        key = (item.source, item.external_id)
        if key in self._seen:
            return False
        self._seen.add(key)
        if len(self._seen) > _SEEN_SET_MAX:
            # keep memory bounded: sets are unordered, so drop everything but the
            # current key; missed re-ingests are caught by vector dedup and the
            # daily limit anyway
            self._seen.clear()
            self._seen.add(key)

        if clear:
            await repo.counter_increment(self._pool, repo.COUNTER_CLEARED)
            log.info("cleared: source=%s title=%r", item.source, item.title[:80])
            return True

        text, fetched = await self._load_text(item)
        if len(text) < self._cfg.fetcher.min_text_length:
            await repo.counter_increment(self._pool, repo.COUNTER_FAILED)
            log.warning("text too short, skipped: source=%s title=%r", item.source, item.title[:80])
            return True

        feed_item = FeedItem(
            source=item.source,
            external_id=item.external_id,
            title=item.title,
            text=text,
            url=item.link,
            published_at=item.published_at,
            full_text_fetched=fetched,
        )
        self._queue.put_nowait(feed_item)
        log.info("new item queued: source=%s title=%r", item.source, item.title[:80])
        return True

    async def _load_text(self, item: NewsItem) -> tuple[str, bool]:
        fetched_text = await fetch_article(
            self._http,
            item.link,
            timeout=self._cfg.fetcher.timeout_seconds,
            retries=self._cfg.fetcher.retries,
        )
        if fetched_text:
            return fetched_text, True
        return item.summary, False
