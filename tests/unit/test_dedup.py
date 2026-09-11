from __future__ import annotations

from app.db import repo
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


async def embed_news(pool, news_id: int, embeddings) -> None:
    news = await repo.get_news(pool, news_id)
    await repo.set_news_embedding(pool, news_id, await embeddings.embed(f"{news.title}\n{news.text}"))


async def test_unique_without_candidates(settings, pool, news_factory) -> None:
    llm = FakeLLM()
    service = DedupService(settings, pool, llm, FakeEmbeddings())
    news_id = await news_factory(METRO_TEXT)

    verdict = await service.process(news_id)

    assert verdict.kind == "unique"
    assert llm.json_calls == []


async def test_duplicate_with_llm_verdict(settings, pool, news_factory) -> None:
    first_id = await news_factory(METRO_TEXT)
    second_id = await news_factory(METRO_REPHRASED, title="Метро открыто")
    embeddings = FakeEmbeddings()
    await embed_news(pool, first_id, embeddings)
    llm = FakeLLM()
    llm.push_json({"is_duplicate": True, "reason": "та же новость", "duplicate_of_id": first_id})
    service = DedupService(settings, pool, llm, embeddings)

    verdict = await service.process(second_id)

    assert verdict.kind == "duplicate"
    assert verdict.duplicate_of_id == first_id
    assert len(llm.json_calls) == 1

    news = await repo.get_news(pool, second_id)
    assert news.embedding is not None


async def test_llm_reports_unique_when_candidates_exist(settings, pool, news_factory) -> None:
    first_id = await news_factory(METRO_TEXT)
    embeddings = FakeEmbeddings()
    await embed_news(pool, first_id, embeddings)
    sport_id = await news_factory(SPORT_TEXT, title="Футбол")
    llm = FakeLLM()
    llm.push_json({"is_duplicate": False, "reason": "другое событие", "duplicate_of_id": None})
    service = DedupService(settings, pool, llm, embeddings)

    verdict = await service.process(sport_id)

    assert verdict.kind == "unique"


async def test_llm_error_review_policy(settings, pool, news_factory) -> None:
    first_id = await news_factory(METRO_TEXT)
    embeddings = FakeEmbeddings()
    await embed_news(pool, first_id, embeddings)
    second_id = await news_factory(METRO_REPHRASED)
    service = DedupService(settings, pool, FakeLLM(), embeddings)

    verdict = await service.process(second_id)

    assert verdict.kind == "needs_review"


async def test_llm_error_pass_policy(settings, pool, news_factory) -> None:
    settings.dedup.on_error = "pass"
    first_id = await news_factory(METRO_TEXT)
    embeddings = FakeEmbeddings()
    await embed_news(pool, first_id, embeddings)
    second_id = await news_factory(METRO_REPHRASED)
    service = DedupService(settings, pool, FakeLLM(), embeddings)

    verdict = await service.process(second_id)

    assert verdict.kind == "unique"


async def test_llm_error_drop_policy(settings, pool, news_factory) -> None:
    settings.dedup.on_error = "drop"
    first_id = await news_factory(METRO_TEXT)
    embeddings = FakeEmbeddings()
    await embed_news(pool, first_id, embeddings)
    second_id = await news_factory(METRO_REPHRASED)
    service = DedupService(settings, pool, FakeLLM(), embeddings)

    verdict = await service.process(second_id)

    assert verdict.kind == "dropped"


async def test_invalid_duplicate_of_id_falls_back_to_candidate(settings, pool, news_factory) -> None:
    first_id = await news_factory(METRO_TEXT)
    second_id = await news_factory(METRO_REPHRASED)
    embeddings = FakeEmbeddings()
    await embed_news(pool, first_id, embeddings)
    llm = FakeLLM()
    llm.push_json({"is_duplicate": True, "reason": "дубль", "duplicate_of_id": 999999})
    service = DedupService(settings, pool, llm, embeddings)

    verdict = await service.process(second_id)

    assert verdict.kind == "duplicate"
    assert verdict.duplicate_of_id == first_id
