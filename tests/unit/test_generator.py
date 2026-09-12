from __future__ import annotations

import datetime as dt

from app.generator import MAX_POST_LENGTH, PostGenerator
from tests.mocks import FakeLLM

STYLE = "Ты — админ канала. Пишем живо."


def make_news(text: str = "Содержание новости про открытие моста в городе", title: str = "Новость о мосте"):
    return type(
        "News",
        (),
        {
            "id": 1,
            "title": title,
            "text": text,
            "published_at": dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.timezone.utc),
            "url": "https://example.com/bridge",
        },
    )()


def make_post(post_id: int, text: str, url: str):
    return type(
        "Post",
        (),
        {
            "id": post_id,
            "text": text,
            "tg_url": url,
            "created_at": dt.datetime(2026, 8, 30, 10, 0, tzinfo=dt.timezone.utc),
        },
    )()


async def test_generate_basic(tmp_path) -> None:
    style_file = tmp_path / "style.md"
    style_file.write_text(STYLE, encoding="utf-8")
    llm = FakeLLM()
    llm.push_json({"text": "Мост открыт <b>досрочно</b>", "references_post_ids": []})
    generator = PostGenerator(make_cfg(), llm, style_file)

    draft = await generator.generate(make_news(), related=[])

    assert draft.text == "Мост открыт <b>досрочно</b>"
    assert draft.reference_ids == []
    call = llm.json_calls[0]
    assert STYLE in call["system"]
    assert "Новость о мосте" in call["user"]


def make_cfg():
    from app.config import ContextConfig, LLMConfig, Settings

    return Settings(
        llm=LLMConfig(temperature=0.3, retries=2),
        context=ContextConfig(),
    )


async def test_generate_with_references(tmp_path) -> None:
    style_file = tmp_path / "style.md"
    style_file.write_text(STYLE, encoding="utf-8")
    llm = FakeLLM()
    llm.push_json(
        {
            "text": 'Как мы <a href="https://t.me/ch/5">писали ранее</a>, мост открыт',
            "references_post_ids": [5, 999],
        }
    )
    generator = PostGenerator(make_cfg(), llm, style_file)
    related = [make_post(5, "прошлый пост про мост", "https://t.me/ch/5")]

    draft = await generator.generate(make_news(), related=related)

    assert draft.reference_ids == [5]
    user_prompt = llm.json_calls[0]["user"]
    assert "[id=5]" in user_prompt
    assert "https://t.me/ch/5" in user_prompt


async def test_generate_shortens_long_text(tmp_path) -> None:
    style_file = tmp_path / "style.md"
    style_file.write_text(STYLE, encoding="utf-8")
    llm = FakeLLM()
    llm.push_json({"text": "длинный " * 300, "references_post_ids": []})
    llm.push_json({"text": "Короткий пост про мост " * 12, "references_post_ids": []})
    generator = PostGenerator(make_cfg(), llm, style_file)

    draft = await generator.generate(make_news(), related=[])

    assert len(draft.text) <= MAX_POST_LENGTH
    assert draft.text == ("Короткий пост про мост " * 12).strip()
    assert len(llm.json_calls) == 2


async def test_generate_shorten_rejects_too_short_text(tmp_path) -> None:
    style_file = tmp_path / "style.md"
    style_file.write_text(STYLE, encoding="utf-8")
    llm = FakeLLM()
    original = "Осмысленный текст новости. " * 60
    llm.push_json({"text": original, "references_post_ids": []})
    llm.push_json({"text": "мусор", "references_post_ids": []})
    generator = PostGenerator(make_cfg(), llm, style_file)

    draft = await generator.generate(make_news(), related=[])

    assert 0 < len(draft.text) <= MAX_POST_LENGTH
    assert "мусор" not in draft.text
    assert draft.text.startswith("Осмысленный текст новости.")


async def test_generate_without_style_file(tmp_path) -> None:
    llm = FakeLLM()
    llm.push_json({"text": "пост", "references_post_ids": []})
    generator = PostGenerator(make_cfg(), llm, tmp_path / "missing.md")

    draft = await generator.generate(make_news(), related=[])

    assert draft.text == "пост"
    call = llm.json_calls[0]
    assert "админ" in call["system"]
