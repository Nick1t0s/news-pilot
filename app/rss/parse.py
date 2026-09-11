from __future__ import annotations

import datetime as dt
import html
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser

_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "fbclid", "gclid", "yclid"}
_WS_RE = re.compile(r"\s+")


class FeedError(RuntimeError):
    pass


@dataclass(slots=True)
class NewsItem:
    source: str
    external_id: str
    title: str
    summary: str
    link: str
    published_at: dt.datetime | None = None
    image_urls: list[str] = field(default_factory=list)


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in _TRACKING_PARAMS]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", urlencode(query), ""))


def strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def parse_feed(data: bytes | str, source: str) -> list[NewsItem]:
    parsed = feedparser.parse(data)
    if parsed.bozo and not parsed.entries:
        raise FeedError(f"feed parse error: {parsed.bozo_exception}")
    items: list[NewsItem] = []
    seen_ids: set[str] = set()
    for entry in parsed.entries:
        link = (entry.get("link") or "").strip()
        title = strip_html(entry.get("title") or "").strip()
        if not link or not title:
            continue
        external_id = str(entry.get("id") or entry.get("guid") or "").strip() or normalize_url(link)
        if external_id in seen_ids:
            continue
        seen_ids.add(external_id)
        summary = strip_html(entry.get("summary") or entry.get("description") or "")
        items.append(
            NewsItem(
                source=source,
                external_id=external_id[:512],
                title=title,
                summary=summary,
                link=link,
                published_at=_parse_time(entry.get("published_parsed") or entry.get("updated_parsed")),
                image_urls=_collect_images(entry),
            )
        )
    return items


def _parse_time(struct) -> dt.datetime | None:
    if not struct:
        return None
    try:
        return dt.datetime(*struct[:6], tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def _collect_images(entry) -> list[str]:
    urls: list[str] = []
    for enclosure in entry.get("enclosures") or []:
        if isinstance(enclosure, dict):
            href = enclosure.get("href") or enclosure.get("url")
        else:
            href = getattr(enclosure, "href", None)
        if href:
            urls.append(str(href))
    for key in ("media_content", "media_thumbnail"):
        for media in entry.get(key) or []:
            url = media.get("url") if isinstance(media, dict) else None
            if url:
                urls.append(str(url))
    unique: list[str] = []
    for url in urls:
        if url.startswith(("http://", "https://")) and url not in unique:
            unique.append(url)
    return unique
