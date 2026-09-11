from __future__ import annotations

from app.textutil import cosine_similarity, plain_text, sanitize_telegram_html


def test_sanitize_keeps_allowed_tags() -> None:
    raw = '<b>Жирный</b> и <a href="https://t.me/x/1">ссылка</a>'
    assert sanitize_telegram_html(raw) == raw


def test_sanitize_drops_disallowed_tags_but_keeps_text() -> None:
    raw = '<script>alert(1)</script>Текст <h2>Заголовок</h2>'
    result = sanitize_telegram_html(raw)
    assert "script" not in result
    assert "alert(1)" in result
    assert "Текст" in result
    assert "Заголовок" in result


def test_sanitize_strips_tag_attributes_and_bad_hrefs() -> None:
    raw = '<b style="color:red" onclick="x()">т</b> <a href="javascript:alert(1)">с</a>'
    result = sanitize_telegram_html(raw)
    assert 'style' not in result
    assert 'javascript' not in result
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
