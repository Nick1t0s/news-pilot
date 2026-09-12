from __future__ import annotations

from types import SimpleNamespace

from app.photo.agent import PhotoAgent
from tests.mocks import FakeLLM, completion, tool_call


def make_news(news_id: int = 1):
    return SimpleNamespace(
        id=news_id,
        title="БПЛА атаковали автомобиль",
        text="В Шебекино беспилотник ударил по автомобилю, пострадал мужчина.",
    )


def make_agent(llm: FakeLLM, tavily) -> PhotoAgent:
    from app.config import Settings

    cfg = Settings()
    return PhotoAgent(cfg, llm, tavily, http=None, images_dir=SimpleNamespace())


def make_tavily(results: list[str]):
    class FakeSearch:
        def __init__(self) -> None:
            self.queries: list[str] = []

        async def search(self, query: str) -> list[str]:
            self.queries.append(query)
            return list(results)

    return FakeSearch()


async def test_search_first_batch_does_not_crash() -> None:
    """Regression: UnboundLocalError when the first tool batch contains tavily_image_search."""
    llm = FakeLLM()
    llm.push_chat(completion(None, [tool_call("t1", "tavily_image_search", {"query": "дрон"})]))
    llm.push_chat(completion(None, [tool_call("t2", "select_images", {"ids": []})]))
    agent = make_agent(llm, make_tavily(results=[]))

    result = await agent._collect_inner(make_news())

    assert result == []
    assert llm.chat_calls[0][0]["role"] == "system"
    tool_messages = [m for m in llm.chat_calls[1] if m.get("role") == "tool"]
    assert tool_messages and tool_messages[0]["tool_call_id"] == "t1"


async def test_search_and_select_in_one_batch() -> None:
    llm = FakeLLM()
    llm.push_chat(
        completion(
            None,
            [
                tool_call("t1", "tavily_image_search", {"query": "дрон"}),
                tool_call("t2", "select_images", {"ids": []}),
            ],
        )
    )
    agent = make_agent(llm, make_tavily(results=[]))

    result = await agent._collect_inner(make_news())

    assert result == []


async def test_collect_swallows_crash_and_returns_empty() -> None:
    llm = FakeLLM()
    agent = make_agent(llm, make_tavily(results=[]))

    result = await agent.collect(make_news())

    assert result == []


async def test_select_downloads_chosen_images(tmp_path, monkeypatch) -> None:
    downloaded: list[str] = []

    async def fake_download(http, url, dest_dir, *, base_name, timeout=20.0, retries=2):
        downloaded.append(url)
        path = tmp_path / f"{base_name}.jpg"
        path.write_bytes(b"stub")
        return path

    monkeypatch.setattr("app.photo.agent.download_image", fake_download)
    llm = FakeLLM()
    llm.push_chat(completion(None, [tool_call("t1", "tavily_image_search", {"query": "дрон"})]))
    llm.push_chat(completion(None, [tool_call("t2", "select_images", {"ids": ["img_0"]})]))
    agent = make_agent(llm, make_tavily(results=["https://example.com/img.jpg"]))
    agent._images_dir = tmp_path

    result = await agent._collect_inner(make_news())

    assert downloaded == ["https://example.com/img.jpg"]
    assert result and result[0].source_url == "https://example.com/img.jpg"


async def test_search_limit_is_respected() -> None:
    llm = FakeLLM()
    for i in range(4):
        llm.push_chat(completion(None, [tool_call(f"t{i}", "tavily_image_search", {"query": f"q{i}"})]))
    llm.push_chat(completion(None, [tool_call("t9", "select_images", {"ids": []})]))
    tavily = make_tavily(results=["https://example.com/x.jpg"])
    agent = make_agent(llm, tavily)
    agent._cfg.photo_agent.max_searches = 2

    result = await agent._collect_inner(make_news())

    assert result == []
    assert len(tavily.queries) == 2


async def test_rss_images_are_not_passed_to_agent() -> None:
    """Regression: agent must always search via tavily, never receive article images."""
    news = SimpleNamespace(
        id=1,
        title="БПЛА атаковали автомобиль",
        text="В Шебекино беспилотник ударил по автомобилю, пострадал мужчина.",
        rss_image_urls=["https://example.com/from-article.jpg"],
    )
    llm = FakeLLM()
    llm.push_chat(completion(None, [tool_call("t1", "select_images", {"ids": []})]))
    tavily = make_tavily(results=[])
    agent = make_agent(llm, tavily)

    result = await agent._collect_inner(news)

    assert result == []
    first_user = llm.chat_calls[0][1]["content"]
    assert "from-article.jpg" not in first_user
    assert "Начальных кандидатов" not in first_user
    assert "tavily_image_search" in first_user
    assert tavily.queries == []
