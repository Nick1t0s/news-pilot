from __future__ import annotations

import os

import pytest
import pytest_asyncio

from app.config import (
    ContextConfig,
    DatabaseConfig,
    DedupConfig,
    EmbeddingsConfig,
    FetcherConfig,
    LLMConfig,
    PhotoAgentConfig,
    PipelineConfig,
    PublishConfig,
    RssConfig,
    Settings,
    TavilyConfig,
    TelegramConfig,
)
from app.db.base import create_pool, init_schema

TEST_DSN = os.environ.get("TEST_DSN", "postgresql+asyncpg://USER:PASSWORD@localhost:5432/DBNAME")

_TRUNCATE = "TRUNCATE processing_log, post_references, post_images, posts, news RESTART IDENTITY CASCADE"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database=DatabaseConfig(dsn=TEST_DSN),
        llm=LLMConfig(retries=2, timeout_seconds=10, temperature=0.4),
        embeddings=EmbeddingsConfig(retries=2, timeout_seconds=5),
        rss=RssConfig(poll_interval_seconds=60, feeds=[]),
        fetcher=FetcherConfig(timeout_seconds=5, retries=1, min_text_length=100),
        tavily=TavilyConfig(api_key="", timeout_seconds=5, retries=2),
        telegram=TelegramConfig(bot_token="000000:TEST", channel_id="@testchannel", admin_id=42),
        dedup=DedupConfig(window_days=3, min_similarity=0.35, top_k=5, on_error="review"),
        photo_agent=PhotoAgentConfig(max_iterations=5, max_searches=3, max_images=4),
        context=ContextConfig(window_days=14, top_k=3, min_similarity=0.35),
        publish=PublishConfig(mode="auto", max_per_hour=100, quiet_hours=None, timezone="UTC", moderation_timeout_hours=24),
        pipeline=PipelineConfig(workers=2),
    )


@pytest_asyncio.fixture(scope="session")
async def pool():
    pool = await create_pool(TEST_DSN, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    await init_schema(pool, dimensions=768)
    yield pool
    await pool.close()


@pytest_asyncio.fixture(autouse=True)
async def clean_db(pool):
    async with pool.acquire() as conn:
        await conn.execute(_TRUNCATE)
    yield
    async with pool.acquire() as conn:
        await conn.execute(_TRUNCATE)


@pytest.fixture
def news_factory(pool):
    from app.db import repo
    from app.db.entities import NewsStatus

    async def create(text: str, title: str = "Новость", source: str = "lenta") -> int:
        return await repo.add_news(
            pool,
            source=source,
            external_id=f"guid-{title}-{text[:20]}",
            title=title,
            text=text,
            url="https://example.com/x",
            full_text_fetched=True,
            published_at=None,
            status=NewsStatus.pending,
        )

    return create
