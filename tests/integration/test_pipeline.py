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
from app.pipeline.selection import NewsSelector
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

# covers both the dedup verdict and the batch selection call in the default case
UNIQUE_VERDICT = {"is_duplicate": False, "reason": "уникальна"}
DEFAULT_VERDICT = {"is_duplicate": False, "reason": "уникальна", "selected_index": 1}


def make_settings() -> Settings:
    return Settings(
        database=DatabaseConfig(dsn=TEST_DSN),
        llm=LLMConfig(retries=2, timeout_seconds=10),
        embeddings=EmbeddingsConfig(retries=2, timeout_seconds=5),
        rss=RssConfig(poll_interval_seconds=60, feeds=[]),
        fetcher=FetcherConfig(timeout_seconds=5, retries=1, min_text_length=100),
        tavily=TavilyConfig(api_key=""),
        telegram=TelegramConfig(bot_token="000:TEST", channel_id="@testchannel", admin_id=42),
        dedup=DedupConfig(min_similarity=0.35, on_error="pass"),
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


class FailingSelector:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def select(self, batch):
        raise self._exc


class Bundle:
    def __init__(self, settings: Settings, pool) -> None:
        self.settings = settings
        self.pool = pool
        self.llm = FakeLLM(default_json=dict(DEFAULT_VERDICT))
        self.sender = FakeSender()
        self.embeddings = FakeEmbeddings()
        self.dedup = DedupService(settings, pool, self.llm, self.embeddings)
        self.selection = NewsSelector(pool, self.llm)
        self.photo = StubPhotoAgent()
        self.generator = StubGenerator()
        self.context = ContextSearch(settings, pool, self.embeddings)
        self.publisher = PublishService(settings, pool, self.sender, self.embeddings, http=None)
        self.queue: asyncio.Queue[FeedItem] = asyncio.Queue()
        self.pipeline = Pipeline(
            settings, pool, self.queue,
            self.dedup, self.selection, self.photo, self.context, self.generator, self.publisher,
        )
        self.poller = FeedPoller(settings, None, pool, self.queue)

    async def ingest(self, item: NewsItem) -> FeedItem:
        assert await self.poller.ingest_item(item) is True
        return await self.queue.get()

    async def gate(self, item: NewsItem) -> FeedItem:
        """Ingest + dedup stage; unique items land in the selection queue."""
        feed_item = await self.ingest(item)
        await self.pipeline.dedup_one(feed_item)
        return feed_item

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


def metro_news(external_id: str, title: str = "Метро открыто") -> NewsItem:
    from app.rss.parse import NewsItem

    return NewsItem(
        source="lenta", external_id=external_id, title=title,
        summary="s", link=f"https://example.com/{external_id}",
    )


async def test_dedup_processes_items_concurrently(settings, pool, monkeypatch) -> None:
    settings.pipeline.concurrency = 2
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)

    active = 0
    max_active = 0

    class SlowDedup:
        def __init__(self, inner) -> None:
            self.inner = inner

        async def process(self, item):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.05)
            try:
                return await self.inner.process(item)
            finally:
                active -= 1

    box.pipeline._dedup = SlowDedup(box.dedup)

    runner = asyncio.create_task(box.pipeline.run())
    try:
        for index in range(4):
            await box.poller.ingest_item(metro_news(f"guid-conc-{index}", f"Метро открыто {index}"))
        await asyncio.wait_for(box.queue.join(), timeout=10.0)
    finally:
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner

    assert max_active <= 2, "semaphore must cap dedup concurrency"
    assert max_active > 1, "dedup must run items in parallel"
    # all four unique items landed in the selection queue
    assert box.pipeline.selection_queued() == 4


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
    await box.pipeline.dedup_one(feed_item)
    assert box.pipeline.selection_queued() == 1
    assert feed_item.embedding is not None  # computed once, reused by context search
    await box.pipeline.select_once()
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

    await box.gate(NewsItem(source="lenta", external_id="guid-a", title="Метро открыто", summary="s", link="https://example.com/a"))
    await box.pipeline.select_once()
    await box.drain()

    box.llm.push_json({"is_duplicate": True, "reason": "та же новость"})
    await box.gate(NewsItem(source="lenta", external_id="guid-b", title="Линию метро ввели в эксплуатацию", summary="s2", link="https://example.com/b"))

    assert await box.counter(repo.COUNTER_DUPLICATES) == 1
    assert box.pipeline.selection_queued() == 0
    assert len(box.sender.published) == 1
    assert len(await box.all_posts()) == 1


async def test_full_pipeline_developing_story(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)

    box.generator.draft = PostDraft(text=METRO_TEXT, reference_ids=[])
    await box.gate(NewsItem(source="lenta", external_id="guid-a", title="Метро открыто", summary="s", link="https://example.com/a"))
    await box.pipeline.select_once()
    await box.drain()
    first_post = (await box.all_posts())[0]

    box.generator.draft = PostDraft(
        text="Поток пассажиров растёт, добавили ещё один состав",
        reference_ids=[int(first_post.id)],
    )
    box.llm.push_json({"is_duplicate": False, "reason": "продолжение истории", "selected_index": 1})
    await box.gate(NewsItem(source="lenta", external_id="guid-c", title="Линию метро ввели в эксплуатацию", summary="s2", link="https://example.com/c"))
    await box.pipeline.select_once()
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

    await box.gate(NewsItem(source="lenta", external_id="guid-m", title="Метро открыто", summary="s", link="https://example.com/m"))
    await box.pipeline.select_once()

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

    await box.gate(NewsItem(source="lenta", external_id="guid-mr", title="Метро открыто", summary="s", link="https://example.com/mr"))
    await box.pipeline.select_once()
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

    await box.gate(NewsItem(source="lenta", external_id="guid-p", title="Метро открыто", summary="s", link="https://example.com/p"))
    await box.pipeline.select_once()
    await box.drain()

    assert len(box.sender.published) == 1
    assert box.sender.published[0]["photos"] == []


async def test_writing_retry_recovers(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)
    box.generator.fail_times = 1
    box.generator.draft = PostDraft(text="пост после ретрая", reference_ids=[])

    await box.gate(NewsItem(source="lenta", external_id="guid-r1", title="Метро открыто", summary="s", link="https://example.com/r1"))
    await box.pipeline.select_once()
    await box.drain()

    assert box.generator.calls == 2
    assert box.pipeline.selection_queued() == 0
    assert len(await box.all_posts()) == 1
    assert await box.counter(repo.COUNTER_FAILED) == 0


async def test_writing_retry_exhausted(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)
    box.generator.fail_times = 99

    await box.gate(NewsItem(source="lenta", external_id="guid-r2", title="Метро открыто", summary="s", link="https://example.com/r2"))
    await box.pipeline.select_once()

    assert box.generator.calls == 2
    assert box.pipeline.selection_queued() == 0
    assert await box.all_posts() == []
    assert await box.counter(repo.COUNTER_FAILED) == 1


async def test_photo_selection_reaches_sender(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)
    box.photo.photos = [PhotoRecord(source_url="https://example.com/img1.jpg", data=b"\xff\xd8\xffstub")]
    box.generator.draft = PostDraft(text="пост с фото", reference_ids=[])

    await box.gate(NewsItem(source="lenta", external_id="guid-i", title="Метро открыто", summary="s", link="https://example.com/i"))
    await box.pipeline.select_once()
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

    await box.gate(NewsItem(source="lenta", external_id="guid-st", title="Метро", summary="s", link="https://example.com/st"))
    await box.pipeline.select_once()
    await box.drain()

    text = await build_stats_text(pool, settings, queued_count=0, drafts_count=0)
    assert "Опубликовано" in text
    assert "1 / 1 / 1" in text
    assert "lenta — 1" in text


async def test_stats_shows_queues(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)
    await box.poller.ingest_item(metro_news("pq-1", "Т"))
    await box.poller.ingest_item(metro_news("pq-2", "Т2"))

    assert box.pipeline.queued_count() == 2
    assert box.pipeline.dedup_active() == 0
    assert box.pipeline.selection_queued() == 0
    text = await build_stats_text(
        pool, settings,
        queued_count=0, drafts_count=0,
        processing_queued=box.pipeline.queued_count(),
        processing_active=box.pipeline.dedup_active(),
        selection_queued=box.pipeline.selection_queued(),
    )
    assert "📥 Ожидают дедупа: 2" in text
    assert "⚙️ В дедупе: 0" in text
    assert "⏳ Ждут отбора: 0" in text
    assert "Осталось постов" not in text


async def test_stats_shows_selection_queue(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)
    await box.gate(metro_news("sq-1", "Первая"))
    await box.gate(metro_news("sq-2", "Вторая"))

    assert box.pipeline.selection_queued() == 2
    text = await build_stats_text(
        pool, settings,
        queued_count=0, drafts_count=0,
        selection_queued=box.pipeline.selection_queued(),
    )
    assert "⏳ Ждут отбора: 2" in text


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


async def test_batch_selection_picks_one_and_discards_rest(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)
    for index in range(3):
        await box.gate(metro_news(f"guid-sel-{index}", f"Метро {index}"))
    assert box.pipeline.selection_queued() == 3

    box.llm.push_json({"selected_index": 2, "reason": "самая значимая"})
    chosen = await box.pipeline.select_once()
    await box.drain()

    assert chosen is not None
    assert len(box.sender.published) == 1
    assert await box.counter(repo.COUNTER_NOT_SELECTED) == 2
    assert await box.counter(repo.COUNTER_DUPLICATES) == 0
    assert box.pipeline.selection_queued() == 0


async def test_batch_selection_null_discards_all(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)
    for index in range(3):
        await box.gate(metro_news(f"guid-null-{index}", f"Метро {index}"))

    box.llm.push_json({"selected_index": None, "reason": "все рутинные"})
    chosen = await box.pipeline.select_once()

    assert chosen is None
    assert box.generator.calls == 0
    assert await box.counter(repo.COUNTER_NOT_SELECTED) == 3
    assert await box.all_posts() == []


async def test_selection_error_requeues_batch(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)
    for index in range(3):
        await box.gate(metro_news(f"guid-rq-{index}", f"Метро {index}"))

    box.pipeline._selection = FailingSelector(LLMError("llm down"))
    chosen = await box.pipeline.select_once()

    assert chosen is None
    assert box.pipeline.selection_queued() == 3
    assert await box.counter(repo.COUNTER_NOT_SELECTED) == 0
    assert await box.counter(repo.COUNTER_FAILED) == 0


async def test_selection_single_item_goes_through_model(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)
    await box.gate(metro_news("guid-single", "Метро открыто"))

    box.llm.push_json({"selected_index": None, "reason": "не достойна"})
    chosen = await box.pipeline.select_once()

    # the model was called even for a single item and decided not to publish it
    assert any(call["name"] == "news_selection" for call in box.llm.json_calls)
    assert chosen is None
    assert await box.counter(repo.COUNTER_NOT_SELECTED) == 1
    assert len(await box.all_posts()) == 0


async def test_only_published_posts_are_dedup_candidates(settings, pool, monkeypatch) -> None:
    """Discarded (not selected) items vanish: they are not candidates next time."""
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)
    box.llm.push_json(dict(UNIQUE_VERDICT))
    box.llm.push_json({"selected_index": None, "reason": "неважная"})

    await box.gate(NewsItem(source="lenta", external_id="guid-sk1", title="Метро открыто", summary="s", link="https://example.com/sk1"))
    await box.pipeline.select_once()
    assert await box.counter(repo.COUNTER_NOT_SELECTED) == 1

    box.llm.push_json(dict(UNIQUE_VERDICT))
    await box.gate(NewsItem(source="lenta", external_id="guid-sk2", title="Линию метро ввели", summary="s2", link="https://example.com/sk2"))
    # no candidates (nothing published) -> the gate still runs
    assert "кандидатов нет" in box.llm.json_calls[-1]["user"]
