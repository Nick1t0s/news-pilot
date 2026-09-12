from __future__ import annotations

from app.textutil import (
    cosine_similarity,
    plain_text,
    sanitize_telegram_html,
    truncate_html,
)


def test_sanitize_keeps_allowed_tags() -> None:
    raw = "<b>Жирный</b> и <a href=\"https://example.com/x\">ссылка</a>"
    assert sanitize_telegram_html(raw) == raw


def test_sanitize_drops_tme_links_but_keeps_text() -> None:
    raw = 'Как мы <a href="https://t.me/channel/21">писали ранее</a>'
    result = sanitize_telegram_html(raw)
    assert "t.me" not in result
    assert "писали ранее" in result


def test_sanitize_drops_disallowed_tags_but_keeps_text() -> None:
    raw = "<script>alert(1)</script>Текст <h2>Заголовок</h2>"
    result = sanitize_telegram_html(raw)
    assert "script" not in result
    assert "alert(1)" in result
    assert "Текст" in result
    assert "Заголовок" in result


def test_sanitize_strips_tag_attributes_and_bad_hrefs() -> None:
    raw = '<b style="color:red" onclick="x()">т</b> <a href="javascript:alert(1)">с</a>'
    result = sanitize_telegram_html(raw)
    assert "style" not in result
    assert "javascript" not in result
    assert "<b>т</b>" in result


def test_sanitize_escapes_raw_text() -> None:
    result = sanitize_telegram_html("a < b & c")
    assert result == "a &lt; b &amp; c"


def test_sanitize_closes_broken_tags() -> None:
    result = sanitize_telegram_html("<b>незакрытый")
    assert result.endswith("</b>")


def test_plain_text_removes_tags() -> None:
    assert plain_text("<b>Привет</b> мир") == "Привет мир"


def test_cosine_similarity_identical() -> None:
    a = [1.0, 0.0, 2.0]
    assert abs(cosine_similarity(a, a) - 1.0) < 1e-9


def test_cosine_similarity_orthogonal() -> None:
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_cosine_similarity_mismatched_lengths() -> None:
    assert cosine_similarity([1.0], [1.0, 2.0]) == 0.0


def test_truncate_html_short_input_unchanged() -> None:
    raw = "<b>Короткий</b> текст"
    assert truncate_html(raw, 100) == raw


def test_truncate_html_cuts_to_limit() -> None:
    words = " ".join(["слово"] * 200)
    result = truncate_html(words, 50)
    assert len(plain_text(result)) <= 50
    assert result.startswith("слово")


def test_truncate_html_keeps_links() -> None:
    text = "Как мы писали ранее " + " ".join(["слово"] * 100)
    raw = f'<a href="https://example.com/x">писали ранее</a> {text}'
    result = truncate_html(raw, 30)
    assert '<a href="https://example.com/x">' in result
    assert result.rstrip().endswith("</a>")


def test_truncate_html_closes_open_tags() -> None:
    raw = "Начало <b>жирный " + " ".join(["слово"] * 100)
    result = truncate_html(raw, 10)
    assert result.endswith("</b>")
    assert "<b>" in result


def test_truncate_html_word_boundary() -> None:
    raw = "первое второе третье"
    result = truncate_html(raw, 12)
    assert result == "первое"
