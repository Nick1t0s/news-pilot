from __future__ import annotations

import datetime as dt

from app.config import GeneratorConfig
from app.generator import PostGenerator
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
            "published_at": dt.datetime(2026, 8, 30, 10, 0, tzinfo=dt.timezone.utc),
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


def make_cfg(generator: GeneratorConfig | None = None):
    from app.config import ContextConfig, LLMConfig, Settings

    return Settings(
        _env_file=None,
        llm=LLMConfig(temperature=0.3, retries=2),
        generator=generator or GeneratorConfig(),
        context=ContextConfig(),
    )


async def test_generate_with_references(tmp_path) -> None:
    style_file = tmp_path / "style.md"
    style_file.write_text(STYLE, encoding="utf-8")
    llm = FakeLLM()
    llm.push_json(
        {
            "text": "Как мы писали ранее, мост открыт",
            "references_post_ids": [5, 999],
        }
    )
    generator = PostGenerator(make_cfg(), llm, style_file)
    related = [make_post(5, "прошлый пост про мост", "https://t.me/ch/5")]

    draft = await generator.generate(make_news(), related=related)

    assert draft.reference_ids == [5]
    user_prompt = llm.json_calls[0]["user"]
    assert "[id=5]" in user_prompt
    assert "t.me" not in user_prompt


async def test_generate_strips_bare_tme_urls(tmp_path) -> None:
    style_file = tmp_path / "style.md"
    style_file.write_text(STYLE, encoding="utf-8")
    llm = FakeLLM()
    llm.push_json(
        {
            "text": "Обновление по мосту (https://t.me/c/123/45) и всё",
            "references_post_ids": [],
        }
    )
    generator = PostGenerator(make_cfg(), llm, style_file)

    draft = await generator.generate(make_news(), related=[])

    assert draft.text == "Обновление по мосту и всё"


async def test_generate_shortens_long_text(tmp_path) -> None:
    style_file = tmp_path / "style.md"
    style_file.write_text(STYLE, encoding="utf-8")
    llm = FakeLLM()
    llm.push_json({"text": "длинный " * 300, "references_post_ids": []})
    llm.push_json({"text": "Короткий пост про мост " * 12, "references_post_ids": []})
    generator = PostGenerator(make_cfg(), llm, style_file)

    draft = await generator.generate(make_news(), related=[])

    assert len(draft.text) <= GeneratorConfig().max_length
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

    assert 0 < len(draft.text) <= GeneratorConfig().max_length
    assert "мусор" not in draft.text
    assert draft.text.startswith("Осмысленный текст новости.")


async def test_generate_respects_custom_max_length(tmp_path) -> None:
    style_file = tmp_path / "style.md"
    style_file.write_text(STYLE, encoding="utf-8")
    cfg = make_cfg(GeneratorConfig(max_length=50, min_length=10))
    llm = FakeLLM()
    llm.push_json({"text": "длинный текст про мост " * 10, "references_post_ids": []})
    llm.push_json({"text": "Короткий пост про мост", "references_post_ids": []})
    generator = PostGenerator(cfg, llm, style_file)

    draft = await generator.generate(make_news(), related=[])

    assert draft.text == "Короткий пост про мост"
    assert len(draft.text) <= 50
    assert "не более 50 символов" in llm.json_calls[0]["system"]
    assert "длиннее 50 символов" in llm.json_calls[1]["user"]


async def test_generate_without_style_file(tmp_path) -> None:
    llm = FakeLLM()
    llm.push_json({"text": "пост", "references_post_ids": []})
    generator = PostGenerator(make_cfg(), llm, tmp_path / "missing.md")

    draft = await generator.generate(make_news(), related=[])

    assert draft.text == "пост"
    call = llm.json_calls[0]
    assert "админ" in call["system"]
