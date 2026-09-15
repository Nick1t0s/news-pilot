from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field


@dataclass(slots=True)
class FeedItem:
    """A fetched news item travelling through the pipeline queues."""

    source: str
    external_id: str
    title: str
    text: str
    url: str
    published_at: dt.datetime | None = None
    full_text_fetched: bool = False


@dataclass(slots=True)
class PublishedPost:
    """A published channel post, as read from the DB."""

    id: int
    source: str
    text: str
    embedding: list[float] | None = None
    tg_message_id: int | None = None
    tg_url: str | None = None
    published_at: dt.datetime | None = None


@dataclass(slots=True)
class PhotoRecord:
    source_url: str
    data: bytes | None = None


@dataclass(slots=True)
class PublishJob:
    """A ready post travelling from the pipeline to the publisher."""

    source: str
    text: str
    source_url: str = ""
    photos: list[PhotoRecord] = field(default_factory=list)
    reply_to_message_id: int | None = None
    admin_note: str = ""


@dataclass(slots=True)
class DraftJob:
    """A moderation draft, held in memory only (dies with the process)."""

    id: int
    job: PublishJob
    created_at: dt.datetime
    admin_chat_id: int | None = None
    admin_message_id: int | None = None


@dataclass(slots=True)
class Counter:
    key: str
    value: int
