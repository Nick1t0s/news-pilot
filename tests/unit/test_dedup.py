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
    llm.push_json({"is_duplicate": False, "reason": "уникальна"})
    service = DedupService(settings, pool, llm, FakeEmbeddings())

    verdict, embedding = await service.process(make_item(METRO_TEXT))

    assert verdict.kind == "unique"
    assert embedding
    assert len(llm.json_calls) == 1
    assert "кандидатов нет" in llm.json_calls[0]["user"]
    assert "is_duplicate" in llm.json_calls[0]["schema"]["properties"]


async def test_duplicate_with_llm_verdict(settings, pool, post_factory) -> None:
    await post_factory(METRO_TEXT)
    llm = FakeLLM()
    llm.push_json({"is_duplicate": True, "reason": "та же новость"})
    service = DedupService(settings, pool, llm, FakeEmbeddings())

    verdict, _ = await service.process(make_item(METRO_REPHRASED))

    assert verdict.kind == "duplicate"
    assert verdict.reason == "та же новость"
    assert len(llm.json_calls) == 1


async def test_llm_reports_unique_when_candidates_exist(settings, pool, post_factory) -> None:
    await post_factory(METRO_TEXT)
    llm = FakeLLM()
    llm.push_json({"is_duplicate": False, "reason": "другое событие"})
    service = DedupService(settings, pool, llm, FakeEmbeddings())

    verdict, _ = await service.process(make_item(SPORT_TEXT, title="Футбол"))

    assert verdict.kind == "unique"


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


async def test_embedding_reused_for_context(settings, pool) -> None:
    """process() returns the embedding so context search does not re-embed."""
    llm = FakeLLM()
    llm.push_json({"is_duplicate": False, "reason": "уникальна"})
    service = DedupService(settings, pool, llm, FakeEmbeddings())

    _, embedding = await service.process(make_item(METRO_TEXT))

    assert len(embedding) == 768
    assert any(embedding)


async def test_candidates_come_from_published_posts(settings, pool, post_factory) -> None:
    """Candidates are published posts only; their text goes into the prompt."""
    await post_factory(METRO_TEXT)
    llm = FakeLLM()
    llm.push_json({"is_duplicate": True, "reason": "та же новость"})
    service = DedupService(settings, pool, llm, FakeEmbeddings())

    verdict, _ = await service.process(make_item(METRO_REPHRASED))
    assert verdict.kind == "duplicate"

    user = llm.json_calls[0]["user"]
    assert "КАНДИДАТЫ (ранее опубликованные посты):" in user
    assert METRO_TEXT[:80] in user


async def test_prompt_has_no_publish_context(settings, pool) -> None:
    """The publication context moved to the batch selection prompt."""
    llm = FakeLLM()
    llm.push_json({"is_duplicate": False, "reason": "уникальна"})
    service = DedupService(settings, pool, llm, FakeEmbeddings())

    await service.process(make_item(METRO_TEXT))

    assert "КОНТЕКСТ ПУБЛИКАЦИИ" not in llm.json_calls[0]["user"]
    assert "should_publish" not in llm.json_calls[0]["system"]
