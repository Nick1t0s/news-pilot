from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path

import httpx
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.bot.handlers import build_dispatcher
from app.config import get_settings, project_root
from app.context import AppContext
from app.context_search import ContextSearch
from app.db.base import create_pool, init_schema
from app.db import repo
from app.dedup import DedupService
from app.fetcher import fetch_article  # noqa: F401  (kept for monkeypatching in tests)
from app.generator import PostGenerator
from app.logging import setup_logging
from app.photo.agent import PhotoAgent
from app.photo.tavily_client import TavilyImageSearch
from app.pipeline.processor import Pipeline
from app.providers.embeddings import EmbeddingProvider
from app.providers.llm import LLMProvider
from app.publish.sender import TelegramSender
from app.publish.service import PublishService
from app.rss.poller import FeedPoller

log = logging.getLogger("main")


async def run() -> None:
    cfg = get_settings()
    setup_logging(cfg.log_level)

    if not cfg.telegram.bot_token:
        log.error("telegram.bot_token is missing (set TELEGRAM__BOT_TOKEN in .env)")
        raise SystemExit(2)

    pool = await create_pool(cfg.database.dsn, min_size=1, max_size=8)
    await init_schema(pool, cfg.embeddings.dimensions)

    http = httpx.AsyncClient(
        follow_redirects=True,
        timeout=cfg.fetcher.timeout_seconds,
        headers={"User-Agent": "news-pilot/1.0 (+https://github.com/news-pilot)"},
    )
    llm = LLMProvider(cfg.llm)
    embeddings = EmbeddingProvider(cfg.embeddings, http)

    dedup = DedupService(cfg, pool, llm, embeddings)
    tavily = TavilyImageSearch(
        cfg.tavily.api_key,
        timeout=cfg.tavily.timeout_seconds,
        retries=cfg.tavily.retries,
    )
    images_dir = Path(cfg.data_dir) / "images"
    photo_agent = PhotoAgent(cfg, llm, tavily, http, images_dir)
    context_search = ContextSearch(cfg, pool, embeddings)
    generator = PostGenerator(cfg, llm, project_root() / "prompts" / "style.md")

    bot = Bot(cfg.telegram.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    sender = TelegramSender(bot, cfg)
    publisher = PublishService(cfg, pool, sender, embeddings)

    queue: asyncio.Queue[int] = asyncio.Queue()
    pipeline = Pipeline(cfg, pool, queue, dedup, photo_agent, context_search, generator, publisher)
    poller = FeedPoller(cfg, http, pool, queue)

    dp = build_dispatcher(cfg)
    dp["ctx"] = AppContext(cfg=cfg, pool=pool, publisher=publisher, sender=sender)

    await recover(pool, queue)
    await publisher.restore()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    tasks = [
        asyncio.create_task(poller.run_forever(), name="poller"),
        asyncio.create_task(pipeline.run(), name="pipeline"),
        asyncio.create_task(publisher.run_queue_worker(), name="publish-queue"),
        asyncio.create_task(publisher.run_moderation_watch(), name="moderation-watch"),
        asyncio.create_task(dp.start_polling(bot, handle_signals=False), name="telegram"),
    ]
    log.info(
        "news-pilot started: feeds=%d mode=%s",
        len(cfg.rss.feeds),
        cfg.publish.mode,
    )

    await stop.wait()
    log.info("shutting down")
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await bot.session.close()
    await http.aclose()
    await pool.close()
    log.info("stopped")


async def recover(pool, queue: asyncio.Queue[int]) -> None:
    """After restart: drop unfinished work and requeue it for fresh processing."""
    reset_ids = await repo.reset_unfinished_news(pool)
    if reset_ids:
        for news_id in reset_ids:
            queue.put_nowait(news_id)
        log.info("recovery: %d unfinished news reset to pending and requeued", len(reset_ids))
    else:
        log.info("recovery: nothing to reset")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
