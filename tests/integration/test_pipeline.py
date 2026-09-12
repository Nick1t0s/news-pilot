from __future__ import annotations

import asyncio
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
from app.db.entities import News, NewsStatus, Post, PostStatus
from app.dedup import DedupService
from app.generator import PostDraft
from app.photo.agent import PhotoRecord
from app.pipeline.processor import Pipeline
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
        publish=PublishConfig(mode="auto", max_per_hour=100),
    )


class StubPhotoAgent:
    def __init__(self, photos: list[PhotoRecord] | None = None, fail: bool = False) -> None:
        self.photos = photos or []
        self.fail = fail

    async def collect(self, news) -> list[PhotoRecord]:
        if self.fail:
            raise RuntimeError("tavily down")
        return list(self.photos)


class StubGenerator:
    def __init__(self) -> None:
        self.draft = PostDraft(text="пост", reference_ids=[])

    async def generate(self, news, related) -> PostDraft:
        allowed = {post.id for post in related}
        return PostDraft(text=self.draft.text, reference_ids=[
            pid for pid in self.draft.reference_ids if pid in allowed
        ])


class StubContext:
    def __init__(self, settings, pool, embeddings) -> None:
        self.inner = ContextSearch(settings, pool, embeddings)

    async def find(self, news):
        return await self.inner.find(news)


class Bundle:
    def __init__(self, settings, pool) -> None:
        self.settings = settings
        self.pool = pool
        self.llm = FakeLLM()
        self.sender = FakeSender()
        self.embeddings = FakeEmbeddings()
        self.dedup = DedupService(settings, pool, self.llm, self.embeddings)
        self.photo = StubPhotoAgent()
        self.generator = StubGenerator()
        self.context = StubContext(settings, pool, self.embeddings)
        self.publisher = PublishService(settings, pool, self.sender, self.embeddings)
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self.pipeline = Pipeline(
            settings, pool, self.queue,
            self.dedup, self.photo, self.context, self.generator, self.publisher,
        )
        self.poller = FeedPoller(settings, None, pool, self.queue)

    async def ingest(self, item: NewsItem) -> int:
        assert await self.poller.ingest_item(item) is True
        return await self.queue.get()

    async def drain(self) -> None:
        while not self.publisher.queue.empty():
            post_id = self.publisher.queue.get_nowait()
            await self.publisher.approve(post_id)

    async def all_posts(self) -> list[Post]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, news_id, text, embedding, tg_message_id, tg_url, status, published_at, created_at FROM posts ORDER BY id")
        from app.db.repo import _post

        return [_post(row) for row in rows]

    async def news(self, news_id: int) -> News:
        return await repo.get_news(self.pool, news_id)


def patch_fetch(monkeypatch, text: str) -> None:
    async def fake_fetch(*args, **kwargs):
        return text

    monkeypatch.setattr("app.rss.poller.fetch_article", fake_fetch)


async def test_full_pipeline_auto_publish(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)

    item = NewsItem(
        source="lenta", external_id="guid-1", title="Метро открыто",
        summary="summary", link="https://example.com/1",
        published_at=dt_utc(2026, 9, 1, 10, 0),
    )
    assert await box.poller.ingest_item(item) is True
    assert await box.poller.ingest_item(item) is False
    news_id = await box.queue.get()
    await box.pipeline.process(news_id)
    await box.drain()

    news = await box.news(news_id)
    assert news.status == NewsStatus.published
    assert news.full_text_fetched is True
    assert news.embedding is not None
    posts = await box.all_posts()
    assert len(posts) == 1
    post = posts[0]
    assert post.status == PostStatus.published
    assert post.tg_message_id == 101
    assert post.tg_url == "https://t.me/testchannel/101"
    assert post.embedding is not None
    stages = await repo.processing_log_stages(pool, news_id)
    assert {"fetch", "dedup", "photo", "writing", "publish"} <= stages
    assert len(box.sender.published) == 1


async def test_full_pipeline_duplicate(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)

    item = NewsItem(source="lenta", external_id="guid-a", title="Метро открыто", summary="s", link="https://example.com/a")
    first_news_id = await box.ingest(item)
    await box.pipeline.process(first_news_id)
    await box.drain()

    patch_fetch(monkeypatch, METRO_REPHRASED)
    item2 = NewsItem(
        source="lenta", external_id="guid-b", title="Линию метро ввели в эксплуатацию",
        summary="s2", link="https://example.com/b",
    )
    second_news_id = await box.ingest(item2)
    box.llm.push_json({"is_duplicate": True, "reason": "та же новость", "duplicate_of_id": first_news_id})
    await box.pipeline.process(second_news_id)

    news2 = await box.news(second_news_id)
    assert news2.status == NewsStatus.duplicate
    assert news2.duplicate_of_id == first_news_id
    assert len(box.sender.published) == 1


async def test_full_pipeline_developing_story(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)

    item = NewsItem(source="lenta", external_id="guid-a", title="Метро открыто", summary="s", link="https://example.com/a")
    first_news_id = await box.ingest(item)
    box.generator.draft = PostDraft(text=METRO_TEXT, reference_ids=[])
    await box.pipeline.process(first_news_id)
    await box.drain()
    first_post = (await box.all_posts())[0]

    patch_fetch(monkeypatch, METRO_REPHRASED)
    item2 = NewsItem(source="lenta", external_id="guid-c", title="Линию метро ввели в эксплуатацию", summary="s2", link="https://example.com/c")
    second_news_id = await box.ingest(item2)
    box.llm.push_json({"is_duplicate": False, "reason": "продолжение истории", "duplicate_of_id": None})
    box.generator.draft = PostDraft(
        text=f'Как мы <a href="{first_post.tg_url}">писали ранее</a>, поток растёт',
        reference_ids=[int(first_post.id)],
    )
    await box.pipeline.process(second_news_id)
    await box.drain()

    posts = await box.all_posts()
    assert len(posts) == 2
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT post_id, referenced_post_id FROM post_references ORDER BY id LIMIT 1"
        )
    assert row is not None
    assert int(row["post_id"]) == int(posts[1].id)
    assert int(row["referenced_post_id"]) == int(first_post.id)
    assert len(box.sender.published) == 2


async def test_moderation_mode_draft_flow(settings, pool, monkeypatch) -> None:
    settings.publish.mode = "moderation"
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)

    item = NewsItem(source="lenta", external_id="guid-m", title="Метро открыто", summary="s", link="https://example.com/m")
    news_id = await box.ingest(item)
    await box.pipeline.process(news_id)

    news = await box.news(news_id)
    assert news.status == NewsStatus.moderation
    posts = await box.all_posts()
    assert len(posts) == 1
    post = posts[0]
    assert post.status == PostStatus.draft
    assert post.tg_message_id is None
    assert len(box.sender.drafts) == 1
    assert box.sender.published == []

    await box.publisher.reject(post.id, "rejected by admin")
    news = await box.news(news_id)
    assert news.status == NewsStatus.rejected
    assert (await box.all_posts()) == []


async def test_photo_failure_does_not_block(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)
    box.photo.fail = True
    box.generator.draft = PostDraft(text="пост без фото", reference_ids=[])

    item = NewsItem(source="lenta", external_id="guid-p", title="Метро открыто", summary="s", link="https://example.com/p")
    news_id = await box.ingest(item)
    await box.pipeline.process(news_id)
    await box.drain()

    news = await box.news(news_id)
    assert news.status == NewsStatus.published
    assert len(box.sender.published) == 1
    assert box.sender.published[0]["photos"] == []


async def test_photo_selection_saves_images(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)
    box.photo.photos = [PhotoRecord(source_url="https://example.com/img1.jpg", local_path="/tmp/none.jpg")]
    box.generator.draft = PostDraft(text="пост с фото", reference_ids=[])

    item = NewsItem(source="lenta", external_id="guid-i", title="Метро открыто", summary="s", link="https://example.com/i")
    news_id = await box.ingest(item)
    await box.pipeline.process(news_id)
    await box.drain()

    assert len(box.sender.published) == 1
    assert box.sender.published[0]["photos"] == ["/tmp/none.jpg"]
    assert await repo.count_post_images(pool) == 1


async def test_fetch_fallback_to_summary(settings, pool, monkeypatch) -> None:
    async def fake_fetch_none(*args, **kwargs):
        return None

    monkeypatch.setattr("app.rss.poller.fetch_article", fake_fetch_none)
    box = Bundle(settings, pool)

    long_summary = METRO_TEXT + " Дополнительный контекст для длины текста, чтобы пройти минимальный порог проверки."
    item = NewsItem(source="lenta", external_id="guid-f", title="Метро открыто", summary=long_summary, link="https://example.com/f")
    news_id = await box.ingest(item)
    await box.pipeline.process(news_id)
    await box.drain()

    news = await box.news(news_id)
    assert news.full_text_fetched is False
    assert news.text == long_summary


async def test_short_text_marked_failed(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, "коротко")
    poller = FeedPoller(settings, None, pool, asyncio.Queue())
    item = NewsItem(source="lenta", external_id="guid-s", title="Короткая", summary="супер краткий", link="https://example.com/s")

    assert await poller.ingest_item(item) is True

    news = await repo.latest_news_id(pool)
    stored = await repo.get_news(pool, int(news))
    assert stored.status == NewsStatus.failed


async def test_stats_matches_db(settings, pool, monkeypatch) -> None:
    patch_fetch(monkeypatch, METRO_TEXT)
    box = Bundle(settings, pool)

    item = NewsItem(source="lenta", external_id="guid-st", title="Метро", summary="s", link="https://example.com/st")
    news_id = await box.ingest(item)
    await box.pipeline.process(news_id)
    await box.drain()

    text = await build_stats_text(pool, settings, queued_count=0)
    assert "Опубликовано" in text
    assert "1 / 1 / 1" in text
    assert "lenta — 1" in text


def dt_utc(*args):
    return dt.datetime(*args, tzinfo=dt.timezone.utc)
