from __future__ import annotations

import datetime as dt

from app.db.entities import FeedItem
from app.dedup import DedupService
from tests.mocks import FakeEmbeddings, FakeLLM

METRO_TEXT = (
    "В Москве открыли новую линию метро длиной 18 км с шестью станциями. "
    "Пассажирам обещают экономию до двадцати минут в пути."
)
METRO_REPHRASED = (
    "Московская новая линия метро длиной 18 км открылась с шестью станциями. "
    "Экономия для пассажиров до двадцати минут."
)
SPORT_TEXT = (
    "Заря обыграла Спутник со счётом три ноль в матче чемпионата по футболу. "
    "Победный гол забили в добавленное время."
)


def make_item(text: str, title: str = "Метро открыто") -> FeedItem:
    return FeedItem(
        source="lenta",
        external_id=f"guid-{text[:20]}",
        title=title,
        text=text,
        url="https://example.com/x",
        published_at=dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.timezone.utc),
    )


async def test_unique_without_candidates(settings, pool) -> None:
    llm = FakeLLM()
    llm.push_json({"is_duplicate": False, "reason": "уникальна", "duplicate_of_id": None, "should_publish": True, "publish_reason": "важная"})
    service = DedupService(settings, pool, llm, FakeEmbeddings())

    verdict, embedding = await service.process(make_item(METRO_TEXT))

    assert verdict.kind == "unique"
    assert embedding
    assert len(llm.json_calls) == 1
    assert "КОНТЕКСТ ПУБЛИКАЦИИ" in llm.json_calls[0]["user"]
    assert "кандидатов нет" in llm.json_calls[0]["user"]


async def test_duplicate_with_llm_verdict(settings, pool, post_factory) -> None:
    first_id = await post_factory(METRO_TEXT)
    llm = FakeLLM()
    llm.push_json({"is_duplicate": True, "reason": "та же новость", "duplicate_of_id": first_id})
    service = DedupService(settings, pool, llm, FakeEmbeddings())

    verdict, _ = await service.process(make_item(METRO_REPHRASED))

    assert verdict.kind == "duplicate"
    assert verdict.duplicate_of_id == first_id
    assert len(llm.json_calls) == 1


async def test_llm_reports_unique_when_candidates_exist(settings, pool, post_factory) -> None:
    await post_factory(METRO_TEXT)
    llm = FakeLLM()
    llm.push_json({"is_duplicate": False, "reason": "другое событие", "duplicate_of_id": None})
    service = DedupService(settings, pool, llm, FakeEmbeddings())

    verdict, _ = await service.process(make_item(SPORT_TEXT, title="Футбол"))

    assert verdict.kind == "unique"


async def test_llm_error_review_policy(settings, pool, post_factory) -> None:
    await post_factory(METRO_TEXT)
    service = DedupService(settings, pool, FakeLLM(), FakeEmbeddings())

    verdict, _ = await service.process(make_item(METRO_REPHRASED))

    assert verdict.kind == "needs_review"


async def test_llm_error_pass_policy(settings, pool, post_factory) -> None:
    settings.dedup.on_error = "pass"
    await post_factory(METRO_TEXT)
    service = DedupService(settings, pool, FakeLLM(), FakeEmbeddings())

    verdict, _ = await service.process(make_item(METRO_REPHRASED))

    assert verdict.kind == "unique"


async def test_llm_error_drop_policy(settings, pool, post_factory) -> None:
    settings.dedup.on_error = "drop"
    await post_factory(METRO_TEXT)
    service = DedupService(settings, pool, FakeLLM(), FakeEmbeddings())

    verdict, _ = await service.process(make_item(METRO_REPHRASED))

    assert verdict.kind == "dropped"


async def test_invalid_duplicate_of_id_falls_back_to_candidate(settings, pool, post_factory) -> None:
    first_id = await post_factory(METRO_TEXT)
    llm = FakeLLM()
    llm.push_json({"is_duplicate": True, "reason": "дубль", "duplicate_of_id": 999999})
    service = DedupService(settings, pool, llm, FakeEmbeddings())

    verdict, _ = await service.process(make_item(METRO_REPHRASED))

    assert verdict.kind == "duplicate"
    assert verdict.duplicate_of_id == first_id


async def test_skipped_when_should_publish_false(settings, pool) -> None:
    llm = FakeLLM()
    llm.push_json({"is_duplicate": False, "reason": "уникальна", "duplicate_of_id": None, "should_publish": False, "publish_reason": "рутинная новость"})
    service = DedupService(settings, pool, llm, FakeEmbeddings())

    verdict, _ = await service.process(make_item(METRO_TEXT))

    assert verdict.kind == "skipped"
    assert verdict.reason == "рутинная новость"


async def test_skipped_takes_publish_reason(settings, pool) -> None:
    llm = FakeLLM()
    llm.push_json({"is_duplicate": False, "reason": "уникальна", "duplicate_of_id": None, "should_publish": False, "publish_reason": ""})
    service = DedupService(settings, pool, llm, FakeEmbeddings())

    verdict, _ = await service.process(make_item(METRO_TEXT))

    assert verdict.kind == "skipped"
    assert verdict.reason == "уникальна"


async def test_publish_context_includes_last_post_age(settings, pool, post_factory) -> None:
    await post_factory(METRO_TEXT)
    llm = FakeLLM()
    llm.push_json({"is_duplicate": False, "reason": "уникальна", "duplicate_of_id": None, "should_publish": True, "publish_reason": "важная"})
    service = DedupService(settings, pool, llm, FakeEmbeddings())

    await service.process(make_item(METRO_TEXT))

    user = llm.json_calls[0]["user"]
    context = user.split("КОНТЕКСТ ПУБЛИКАЦИИ")[1].split("НОВАЯ НОВОСТЬ")[0]
    assert "Последний пост опубликован в " in context
    assert "назад)." in context
    assert "Балансируй частоту" in context
    assert "только что" in context  # spacing hint references the age


async def test_publish_context_without_last_post(settings, pool) -> None:
    llm = FakeLLM()
    llm.push_json({"is_duplicate": False, "reason": "уникальна", "duplicate_of_id": None, "should_publish": True, "publish_reason": "важная"})
    service = DedupService(settings, pool, llm, FakeEmbeddings())

    await service.process(make_item(METRO_TEXT))

    user = llm.json_calls[0]["user"]
    context = user.split("КОНТЕКСТ ПУБЛИКАЦИИ")[1].split("НОВАЯ НОВОСТЬ")[0]
    assert "Последний пост: неизвестно (постов ещё не публиковалось)." in context


async def test_publish_context_with_limit(settings, pool) -> None:
    settings.limits.daily_posts = 10
    settings.limits.reserve_posts = 3
    llm = FakeLLM()
    llm.push_json({"is_duplicate": False, "reason": "уникальна", "duplicate_of_id": None, "should_publish": True, "publish_reason": "важная"})
    service = DedupService(settings, pool, llm, FakeEmbeddings())

    await service.process(make_item(METRO_TEXT))

    user = llm.json_calls[0]["user"]
    assert "из 10 (лимит)" in user
    assert "Осталось: 10" in user
    assert "резерв (~3 постов)" in user
    assert "РАВНОМЕРНО" in user


async def test_publish_context_without_limit(settings, pool) -> None:
    settings.limits.daily_posts = 0
    llm = FakeLLM()
    llm.push_json({"is_duplicate": False, "reason": "уникальна", "duplicate_of_id": None, "should_publish": True, "publish_reason": "важная"})
    service = DedupService(settings, pool, llm, FakeEmbeddings())

    await service.process(make_item(METRO_TEXT))

    user = llm.json_calls[0]["user"]
    assert "лимит на посты в день не задан" in user
    context = user.split("КОНТЕКСТ ПУБЛИКАЦИИ")[1].split("НОВАЯ НОВОСТЬ")[0]
    assert "(лимит)" not in context  # no hard limit -> no "published X of Y" line


async def test_candidates_come_from_published_posts(settings, pool, post_factory) -> None:
    """Candidates are published posts only; their text goes into the prompt."""
    await post_factory(METRO_TEXT)
    llm = FakeLLM()
    llm.push_json({"is_duplicate": True, "reason": "та же новость", "duplicate_of_id": 1})
    service = DedupService(settings, pool, llm, FakeEmbeddings())

    verdict, _ = await service.process(make_item(METRO_REPHRASED))
    assert verdict.kind == "duplicate"

    user = llm.json_calls[0]["user"]
    assert "КАНДИДАТЫ (ранее опубликованные посты):" in user
    assert METRO_TEXT[:80] in user
