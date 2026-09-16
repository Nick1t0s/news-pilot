from __future__ import annotations

import datetime as dt

import pytest

from app.db.entities import FeedItem
from app.pipeline.selection import (
    _BATCH_TEXT_LIMIT,
    NewsSelector,
    PublishContext,
    SelectionError,
)
from tests.mocks import FakeLLM

NEWS_TEXTS = [
    "Правительство утвердило новую программу поддержки промышленности на три года.",
    "Открыта первая линия нового трамвайного маршрута через центр города.",
    "Учёные получили грант на изучение арктических почв и мерзлоты.",
]


def make_batch(count: int = 3) -> list[FeedItem]:
    return [
        FeedItem(
            source="lenta",
            external_id=f"guid-{index}",
            title=f"Новость номер {index}",
            text=NEWS_TEXTS[index % len(NEWS_TEXTS)],
            url=f"https://example.com/{index}",
            published_at=dt.datetime(2026, 9, 1, 10, index, tzinfo=dt.timezone.utc),
        )
        for index in range(count)
    ]


def make_selector(settings, pool, llm) -> NewsSelector:
    return NewsSelector(pool, llm)


async def test_select_returns_chosen_item(settings, pool) -> None:
    batch = make_batch()
    llm = FakeLLM()
    llm.push_json({"selected_index": 2, "reason": "самая значимая"})
    selector = make_selector(settings, pool, llm)

    chosen = await selector.select(batch)

    assert chosen is batch[1]
    user = llm.json_calls[0]["user"]
    assert "КОНТЕКСТ ПУБЛИКАЦИИ" in user
    assert "БАТЧ УНИКАЛЬНЫХ НОВОСТЕЙ" in user
    assert "[1] " in user and "[3] " in user
    assert "Новость номер 0" in user
    assert "selected_index от 1 до 3" in user
    assert "news_selection" in llm.json_calls[0]["name"]


async def test_select_null_means_nothing_to_publish(settings, pool) -> None:
    llm = FakeLLM()
    llm.push_json({"selected_index": None, "reason": "все рутинные"})
    selector = make_selector(settings, pool, llm)

    assert await selector.select(make_batch()) is None


async def test_select_empty_batch_returns_none(settings, pool) -> None:
    llm = FakeLLM()
    selector = make_selector(settings, pool, llm)

    assert await selector.select([]) is None
    assert llm.json_calls == []


@pytest.mark.parametrize("raw", [0, 99, "abc"])
async def test_select_invalid_index_raises(settings, pool, raw) -> None:
    llm = FakeLLM()
    llm.push_json({"selected_index": raw, "reason": "..."})
    selector = make_selector(settings, pool, llm)

    with pytest.raises(SelectionError):
        await selector.select(make_batch())


async def test_select_llm_failure_raises_selection_error(settings, pool) -> None:
    selector = make_selector(settings, pool, FakeLLM())  # no scripted results

    with pytest.raises(SelectionError):
        await selector.select(make_batch())


async def test_batch_text_is_truncated(settings, pool) -> None:
    long_text = "Слово " * 500
    batch = make_batch(1)
    batch[0].text = long_text
    llm = FakeLLM()
    llm.push_json({"selected_index": 1, "reason": "..."})
    selector = make_selector(settings, pool, llm)

    await selector.select(batch)

    user = llm.json_calls[0]["user"]
    assert long_text[:_BATCH_TEXT_LIMIT] in user
    assert long_text.strip() not in user


async def test_publish_context_includes_last_post_age(settings, pool, post_factory) -> None:
    await post_factory("Метро открыли раньше срока и обещают экономию времени пассажирам")
    context = PublishContext(pool)

    text = await context.build()

    assert "КОНТЕКСТ ПУБЛИКАЦИИ" in text
    assert "Последний пост опубликован в " in text
    assert "назад)." in text
    assert "равномерно" in text


async def test_publish_context_without_last_post(settings, pool) -> None:
    context = PublishContext(pool)

    text = await context.build()

    assert "Последний пост: неизвестно (постов ещё не публиковалось)." in text
    assert "(лимит)" not in text
    assert "reserve_posts" not in text
