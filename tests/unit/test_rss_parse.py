from __future__ import annotations

from pathlib import Path

import pytest

from app.rss.parse import FeedError, NewsItem, normalize_url, parse_feed, strip_html

FIXTURES = Path(__file__).parent.parent / "fixtures" / "rss"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_parse_full_feed() -> None:
    items = parse_feed(load("basic.xml"), "lenta")
    assert len(items) == 2

    first = items[0]
    assert isinstance(first, NewsItem)
    assert first.source == "lenta"
    assert first.external_id == "guid-1"
    assert first.title == "Первая новость"
    assert first.summary == "Краткое описание первой новости"
    assert first.link == "https://example.com/1"
    assert first.published_at is not None


def test_parse_without_guid_uses_normalized_link() -> None:
    items = parse_feed(load("no_guid.xml"), "ria")
    assert len(items) == 1
    item = items[0]
    assert item.external_id == normalize_url("https://example.com/news/2?utm_source=rss&id=9")
    assert item.external_id == "https://example.com/news/2?id=9"


def test_normalize_url_strips_tracking() -> None:
    assert normalize_url("https://Example.com/a/?utm_medium=rss&keep=1") == "https://example.com/a?keep=1"


def test_duplicate_guid_in_feed_skipped() -> None:
    items = parse_feed(load("duplicates_inside.xml"), "lenta")
    assert len(items) == 1


def test_invalid_feed_raises() -> None:
    with pytest.raises(FeedError):
        parse_feed(b"<rss><channel>", "broken")


def test_strip_html() -> None:
    assert strip_html("<p>Привет &amp; пока <b>жирный</b></p>  ") == "Привет & пока жирный"
