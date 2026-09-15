from __future__ import annotations

import asyncio
import contextlib
import datetime as dt

from app.bot.stats import build_stats_text
from app.config import (
    ContextConfig,
    DatabaseConfig,
    DedupConfig,
    EmbeddingsConfig,
    FetcherConfig,
    LLMConfig,
    PhotoAgentConfig,
    PublishConfig,
    RssConfig,
    Settings,
    TavilyConfig,
    TelegramConfig,
)
from app.context_search import ContextSearch
from app.db import repo
from app.db.entities import FeedItem, PublishedPost
from app.dedup import DedupService
from app.generator import PostDraft
from app.photo.agent import PhotoRecord
from app.pipeline.processor import Pipeline
from app.providers.llm import LLMError
from app.publish.service import PublishService
from app.rss.parse import NewsItem
from app.rss.poller import FeedPoller
from tests.conftest import TEST_DSN
from tests.mocks import FakeEmbeddings, FakeLLM, FakeSender

METRO_TEXT = (
    "В Москве открыли новую линию метро длиной 18 км с шестью станциями. "
    "Пассажирам обещают экономию до двадцати минут в пути, проект обошёлся в 150 млрд рублей."
)
METRO_REPHRASED = (
    "Новая московская линия метро длиной 18 км открылась с шестью станциями. "
    "Для пассажиров обещана экономия до двадцати минут, стоимость составила 150 млрд рублей."
)

UNIQUE_VERDICT = {
    "is_duplicate": False, "reason": "unique", "duplicate_of_id": None,
    "should_publish": True, "publish_reason": "important",
}


def make_settings() -> Settings:
    return Settings(
        database=DatabaseConfig(dsn=TEST_DSN),
        llm=LLMConfig(retries=2, timeout_seconds=10),
        embeddings=EmbeddingsConfig(retries=2, timeout_seconds=5),
        rss=RssConfig(poll_interval_seconds=60, feeds=[]),
        fetcher=FetcherConfig(timeout_seconds=5, retries=1, min_text_length=100),
        tavily=TavilyConfig(api_key=""),
        telegram=TelegramConfig(bot_token="000:TEST", channel_id="@testchannel", admin_id=42),
        dedup=DedupConfig(min_similarity=0.35, on_error="review"),
        photo_agent=PhotoAgentConfig(),
        context=ContextConfig(min_similarity=0.35),
        publish=PublishConfig(mode="auto", moderation_timeout_hours=24),
    )


class StubPhotoAgent:
    def __init__(self, photos: list[PhotoRecord] | None = None, fail: bool = False) -> None:
        self.photos = photos or []
        self.fail = fail

    async def collect(self, item) -> list[PhotoRecord]:
        if self.fail:
            raise RuntimeError("tavily down")
        return list(self.photos)


class StubGenerator:
    def __init__(self, fail_times: int = 0) -> None:
        self.draft = PostDraft(text="пост", reference_ids=[])
        self.fail_times = fail_times
        self.calls = 0

    async def generate(self, item, related) -> PostDraft:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise LLMError("generator returned empty post text")
        allowed = {post.id for post in related}
        return PostDraft(text=self.draft.text, reference_ids=[
            pid for pid in self.draft.reference_ids if pid in allowed
        ])


class Bundle:
    def __init__(self, settings: Settings, pool) -> None:
        self.settings = settings
        self.pool = pool
        self.llm = FakeLLM(default_json=dict(UNIQUE_VERDICT))
        self.sender = FakeSender()
        self.embeddings = FakeEmbeddings()
        self.dedup = DedupService(settings, pool, self.llm, self.embeddings)
        self.photo = StubPhotoAgent()
        self.generator = StubGenerator()
        self.context = ContextSearch(settings, pool, self.embeddings)
        self.publisher = PublishService(settings, pool, self.sender, self.embeddings, http=None)
        self.queue: asyncio.Queue[FeedItem] = asyncio.Queue()
        self.pipeline = Pipeline(
            settings, pool, self.queue,
            self.dedup, self.photo, self.context, self.generator, self.publisher,
        )
        self.poller = FeedPoller(settings, None, pool, self.queue)

    async def ingest(self, item: NewsItem) -> FeedItem:
        assert await self.poller.ingest_item(item) is True
        return await self.queue.get()

    async def drain(self) -> None:
        while not self.publisher.queue.empty():
            job = self.publisher.queue.get_nowait()
            await self.publisher._publish(job, id(job))

    async def all_posts(self) -> list[PublishedPost]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, source, text, embedding, tg_message_id, tg_url, published_at"
                " FROM posts ORDER BY id"
            )
        from app.db.repo import _post

        return [_post(row) for row in rows]

    async def counter(self, key: str) -> int:
        return await repo.counter_get(self.pool, key)


def patch_fetch(monkeypatch, text: str) -> None:
    async def fake_fetch(*args, **kwargs):
        return text

    monkeypatch.setattr("app.rss.poller.fetch_article", fake_fetch)


async def test_pipeline_processes_items_concurrently(settings, pool, monkeypatch) -> None:
    settings.pipeline.concurrency = 2
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)

    active = 0
    max_active = 0

    class SlowGenerator(StubGenerator):
        async def generate(self, item, related) -> PostDraft:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.05)
            try:
                return await super().generate(item, related)
            finally:
                active -= 1

    box.generator = SlowGenerator()
    box.pipeline._generator = box.generator  # pipeline keeps its own reference

    runner = asyncio.create_task(box.pipeline.run())
    try:
        for index in range(4):
            item = NewsItem(
                source="lenta", external_id=f"guid-conc-{index}", title=f"Метро открыто {index}",
                summary="s", link=f"https://example.com/conc-{index}",
            )
            await box.poller.ingest_item(item)
        await asyncio.wait_for(box.queue.join(), timeout=10.0)
    finally:
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner

    assert max_active <= 2, "semaphore must cap concurrency"
    assert max_active > 1, "news must be processed in parallel"
    # all four items passed the gate and landed in the publish queue
    assert box.publisher.queued_count() == 4


async def test_full_pipeline_auto_publish(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)

    item = NewsItem(
        source="lenta", external_id="guid-1", title="Метро открыто",
        summary="summary", link="https://example.com/1",
        published_at=dt_utc(2026, 9, 1, 10, 0),
    )
    assert await box.poller.ingest_item(item) is True
    assert await box.poller.ingest_item(item) is False  # seen set absorbs re-ingest
    feed_item = await box.queue.get()
    assert feed_item.full_text_fetched is True
    assert feed_item.text == METRO_TEXT
    await box.pipeline.process(feed_item)
    await box.drain()

    posts = await box.all_posts()
    assert len(posts) == 1
    post = posts[0]
    assert post.source == "lenta"
    assert post.tg_message_id == 101
    assert post.tg_url == "https://t.me/testchannel/101"
    assert post.embedding is not None
    assert post.published_at is not None
    assert len(box.sender.published) == 1


async def test_full_pipeline_duplicate(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)

    first = await box.ingest(NewsItem(source="lenta", external_id="guid-a", title="Метро открыто", summary="s", link="https://example.com/a"))
    await box.pipeline.process(first)
    await box.drain()
    first_post = (await box.all_posts())[0]

    patch_fetch(monkeypatch, METRO_REPHRASED)
    second = await box.ingest(NewsItem(source="lenta", external_id="guid-b", title="Линию метро ввели в эксплуатацию", summary="s2", link="https://example.com/b"))
    box.llm.push_json({"is_duplicate": True, "reason": "та же новость", "duplicate_of_id": first_post.id})
    await box.pipeline.process(second)

    assert await box.counter(repo.COUNTER_DUPLICATES) == 1
    assert len(box.sender.published) == 1
    assert len(await box.all_posts()) == 1


async def test_full_pipeline_developing_story(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)

    first = await box.ingest(NewsItem(source="lenta", external_id="guid-a", title="Метро открыто", summary="s", link="https://example.com/a"))
    box.generator.draft = PostDraft(text=METRO_TEXT, reference_ids=[])
    await box.pipeline.process(first)
    await box.drain()
    first_post = (await box.all_posts())[0]

    patch_fetch(monkeypatch, METRO_REPHRASED)
    second = await box.ingest(NewsItem(source="lenta", external_id="guid-c", title="Линию метро ввели в эксплуатацию", summary="s2", link="https://example.com/c"))
    box.llm.push_json({"is_duplicate": False, "reason": "продолжение истории", "duplicate_of_id": None})
    box.generator.draft = PostDraft(
        text="Поток пассажиров растёт, добавили ещё один состав",
        reference_ids=[int(first_post.id)],
    )
    await box.pipeline.process(second)
    await box.drain()

    posts = await box.all_posts()
    assert len(posts) == 2
    # second post was sent as a reply to the freshest referenced post
    assert box.sender.published[-1]["reply_to"] == box.sender.published[-2]["message_id"]
    # no post_references table: the link travelled inside the PublishJob


async def test_moderation_mode_draft_flow(settings, pool, monkeypatch) -> None:
    settings.publish.mode = "moderation"
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)

    item = await box.ingest(NewsItem(source="lenta", external_id="guid-m", title="Метро открыто", summary="s", link="https://example.com/m"))
    await box.pipeline.process(item)

    assert box.publisher.drafts_count() == 1
    assert box.publisher.queued_count() == 0
    assert len(box.sender.drafts) == 1
    assert box.sender.published == []
    assert "обработано за" in box.sender.drafts[0]["text"]
    assert await box.all_posts() == []

    draft_id = next(iter(box.publisher._drafts))
    await box.publisher.approve_draft(draft_id)

    posts = await box.all_posts()
    assert len(posts) == 1
    assert posts[0].tg_message_id == box.sender.published[-1]["message_id"]
    assert box.publisher.drafts_count() == 0


async def test_moderation_reject(settings, pool, monkeypatch) -> None:
    settings.publish.mode = "moderation"
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)

    item = await box.ingest(NewsItem(source="lenta", external_id="guid-mr", title="Метро открыто", summary="s", link="https://example.com/mr"))
    await box.pipeline.process(item)
    draft_id = next(iter(box.publisher._drafts))

    await box.publisher.reject_draft(draft_id, "rejected by admin")

    assert box.publisher.drafts_count() == 0
    assert await box.all_posts() == []
    assert box.sender.published == []
    assert await box.counter(repo.COUNTER_REJECTED) == 1


async def test_approve_unknown_draft_raises(settings, pool) -> None:
    """Drafts live in memory only: after restart they are gone."""
    box = Bundle(settings, pool)
    service = PublishService(settings, pool, box.sender, box.embeddings, http=None)

    import pytest

    with pytest.raises(LookupError):
        await service.approve_draft(123)


async def test_photo_failure_does_not_block(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)
    box.photo.fail = True
    box.generator.draft = PostDraft(text="пост без фото", reference_ids=[])

    item = await box.ingest(NewsItem(source="lenta", external_id="guid-p", title="Метро открыто", summary="s", link="https://example.com/p"))
    await box.pipeline.process(item)
    await box.drain()

    assert len(box.sender.published) == 1
    assert box.sender.published[0]["photos"] == []


async def test_pipeline_retry_recovers(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)
    box.generator.fail_times = 1
    box.generator.draft = PostDraft(text="пост после ретрая", reference_ids=[])

    item = await box.ingest(NewsItem(source="lenta", external_id="guid-r1", title="Метро открыто", summary="s", link="https://example.com/r1"))
    await box.pipeline.process(item)
    await box.drain()

    assert box.generator.calls == 2
    assert box.queue.empty()
    assert len(await box.all_posts()) == 1
    assert await box.counter(repo.COUNTER_FAILED) == 0


async def test_pipeline_retry_exhausted(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)
    box.generator.fail_times = 99

    item = await box.ingest(NewsItem(source="lenta", external_id="guid-r2", title="Метро открыто", summary="s", link="https://example.com/r2"))
    await box.pipeline.process(item)

    assert box.generator.calls == 2
    assert box.queue.empty()
    assert await box.all_posts() == []
    assert await box.counter(repo.COUNTER_FAILED) == 1


async def test_photo_selection_reaches_sender(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)
    box.photo.photos = [PhotoRecord(source_url="https://example.com/img1.jpg", data=b"\xff\xd8\xffstub")]
    box.generator.draft = PostDraft(text="пост с фото", reference_ids=[])

    item = await box.ingest(NewsItem(source="lenta", external_id="guid-i", title="Метро открыто", summary="s", link="https://example.com/i"))
    await box.pipeline.process(item)
    await box.drain()

    assert len(box.sender.published) == 1
    assert box.sender.published[0]["photos"] == [("https://example.com/img1.jpg", b"\xff\xd8\xffstub")]
    # photos are not persisted anywhere: only the published post row exists
    assert await box.all_posts()


async def test_fetch_fallback_to_summary(settings, pool, monkeypatch) -> None:
    async def fake_fetch_none(*args, **kwargs):
        return None

    monkeypatch.setattr("app.rss.poller.fetch_article", fake_fetch_none)
    box = Bundle(settings, pool)

    long_summary = METRO_TEXT + " Дополнительный контекст для длины текста, чтобы пройти минимальный порог проверки."
    item = await box.ingest(NewsItem(source="lenta", external_id="guid-f", title="Метро открыто", summary=long_summary, link="https://example.com/f"))

    assert item.full_text_fetched is False
    assert item.text == long_summary


async def test_short_text_counts_failed(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, "коротко")
    poller = FeedPoller(settings, None, pool, asyncio.Queue())
    item = NewsItem(source="lenta", external_id="guid-s", title="Короткая", summary="супер краткий", link="https://example.com/s")

    assert await poller.ingest_item(item) is True
    assert await repo.counter_get(pool, repo.COUNTER_FAILED) == 1


async def test_stats_matches_db(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)

    item = await box.ingest(NewsItem(source="lenta", external_id="guid-st", title="Метро", summary="s", link="https://example.com/st"))
    await box.pipeline.process(item)
    await box.drain()

    text = await build_stats_text(pool, settings, queued_count=0, drafts_count=0)
    assert "Опубликовано" in text
    assert "1 / 1 / 1" in text
    assert "lenta — 1" in text


async def test_stats_shows_processing_queue(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)
    await box.poller.ingest_item(NewsItem(source="lenta", external_id="pq-1", title="Т", summary="s", link="https://example.com/pq-1"))
    await box.poller.ingest_item(NewsItem(source="lenta", external_id="pq-2", title="Т2", summary="s", link="https://example.com/pq-2"))

    assert box.pipeline.queued_count() == 2
    assert box.pipeline.in_flight_count() == 0
    text = await build_stats_text(
        pool, settings,
        queued_count=0, drafts_count=0,
        processing_queued=box.pipeline.queued_count(),
        processing_active=box.pipeline.in_flight_count(),
    )
    assert "📥 Ожидают обработки: 2" in text
    assert "⚙️ В работе: 0" in text
    # conftest settings keep the default limit (daily_posts=50): nothing published yet
    assert "Осталось постов на сегодня: 50 из 50" in text


async def test_stats_shows_daily_remaining(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, METRO_TEXT)
    settings.limits.daily_posts = 5
    box = Bundle(settings, pool)
    item = await box.ingest(NewsItem(source="lenta", external_id="rem-1", title="Метро", summary="s", link="https://example.com/rem-1"))
    await box.pipeline.process(item)
    await box.drain()

    text = await build_stats_text(pool, settings, queued_count=0, drafts_count=0)
    assert "Осталось постов на сегодня: 4 из 5" in text


def dt_utc(*args):
    return dt.datetime(*args, tzinfo=dt.timezone.utc)


async def test_clear_run_skips_items(settings, pool) -> None:
    queue: asyncio.Queue[FeedItem] = asyncio.Queue()
    poller = FeedPoller(settings, None, pool, queue)
    item = NewsItem(
        source="lenta", external_id="guid-clear-1", title="Старая новость",
        summary="старый summary", link="https://example.com/clear-1",
    )

    assert await poller.ingest_item(item, clear=True) is True
    assert await poller.ingest_item(item, clear=True) is False

    assert queue.empty()
    assert await repo.counter_get(pool, repo.COUNTER_CLEARED) == 1


async def test_normal_ingest_after_clear_run(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, METRO_TEXT)
    queue: asyncio.Queue[FeedItem] = asyncio.Queue()
    poller = FeedPoller(settings, None, pool, queue)
    old = NewsItem(source="lenta", external_id="guid-old", title="Старая", summary="s", link="https://example.com/old")
    fresh = NewsItem(source="lenta", external_id="guid-fresh", title="Свежая", summary="s", link="https://example.com/fresh")

    assert await poller.ingest_item(old, clear=True) is True
    assert await poller.ingest_item(fresh) is True

    feed_item = await queue.get()
    assert feed_item.external_id == "guid-fresh"
    assert queue.empty()


async def test_daily_limit_skips_news_without_llm(settings, pool, monkeypatch) -> None:
    settings.limits.daily_posts = 1
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)
    box.llm.push_json(dict(UNIQUE_VERDICT))

    first = await box.ingest(NewsItem(source="lenta", external_id="guid-lim-1", title="Метро открыто", summary="s", link="https://example.com/lim-1"))
    await box.pipeline.process(first)
    await box.drain()
    assert len(await box.all_posts()) == 1
    assert len(box.llm.json_calls) == 1

    second = await box.ingest(NewsItem(source="lenta", external_id="guid-lim-2", title="Футбол", summary="s", link="https://example.com/lim-2"))
    await box.pipeline.process(second)

    assert await box.counter(repo.COUNTER_LIMIT_SKIPPED) == 1
    assert len(box.llm.json_calls) == 1
    assert len(await box.all_posts()) == 1


async def test_importance_gate_skips_news(settings, pool, monkeypatch) -> None:
    settings.limits.daily_posts = 5
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)
    box.llm.push_json({"is_duplicate": False, "reason": "уникальна", "duplicate_of_id": None, "should_publish": False, "publish_reason": "рутинная новость"})

    item = await box.ingest(NewsItem(source="lenta", external_id="guid-skip-1", title="Метро открыто", summary="s", link="https://example.com/skip-1"))
    await box.pipeline.process(item)

    assert await box.counter(repo.COUNTER_SKIPPED_UNIMPORTANT) == 1
    assert await box.all_posts() == []
    assert box.generator.calls == 0


async def test_no_limit_when_daily_posts_zero(settings, pool, monkeypatch) -> None:
    settings.limits.daily_posts = 0
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)

    item = await box.ingest(NewsItem(source="lenta", external_id="guid-nolim", title="Метро открыто", summary="s", link="https://example.com/nolim"))
    await box.pipeline.process(item)
    await box.drain()

    assert len(box.sender.published) == 1
    assert len(await box.all_posts()) == 1


async def test_only_published_posts_are_dedup_candidates(settings, pool, monkeypatch) -> None:
    """Skipped items vanish entirely: they are not candidates next time."""
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)
    box.llm.push_json({"is_duplicate": False, "reason": "уникальна", "duplicate_of_id": None, "should_publish": False, "publish_reason": "неважная"})

    first = await box.ingest(NewsItem(source="lenta", external_id="guid-sk1", title="Метро открыто", summary="s", link="https://example.com/sk1"))
    await box.pipeline.process(first)
    assert await box.counter(repo.COUNTER_SKIPPED_UNIMPORTANT) == 1

    patch_fetch(monkeypatch, METRO_REPHRASED)
    second = await box.ingest(NewsItem(source="lenta", external_id="guid-sk2", title="Линию метро ввели", summary="s2", link="https://example.com/sk2"))
    # no candidates (nothing published) -> still asks the gate; unique verdict default
    await box.pipeline.process(second)
    assert "кандидатов нет" in box.llm.json_calls[-1]["user"]
