from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass


class NewsStatus(str, enum.Enum):
    pending = "pending"
    dedup = "dedup"
    photo_search = "photo_search"
    writing = "writing"
    queued = "queued"
    moderation = "moderation"
    published = "published"
    duplicate = "duplicate"
    needs_review = "needs_review"
    rejected = "rejected"
    failed = "failed"
    cleared = "cleared"


class PostStatus(str, enum.Enum):
    draft = "draft"
    queued = "queued"
    published = "published"
    failed = "failed"


@dataclass(slots=True)
class News:
    id: int
    source: str
    external_id: str
    title: str
    text: str
    url: str
    full_text_fetched: bool = False
    published_at: dt.datetime | None = None
    embedding: list[float] | None = None
    status: NewsStatus = NewsStatus.pending
    duplicate_of_id: int | None = None
    created_at: dt.datetime | None = None


@dataclass(slots=True)
class Post:
    id: int
    news_id: int
    text: str
    embedding: list[float] | None = None
    tg_message_id: int | None = None
    tg_url: str | None = None
    status: PostStatus = PostStatus.draft
    published_at: dt.datetime | None = None
    created_at: dt.datetime | None = None


@dataclass(slots=True)
class PostImage:
    id: int
    post_id: int
    source_url: str
    local_path: str | None
    position: int


@dataclass(slots=True)
class PostReference:
    id: int
    post_id: int
    referenced_post_id: int


@dataclass(slots=True)
class LogEntry:
    id: int
    news_id: int
    stage: str
    level: str
    message: str
    created_at: dt.datetime | None = None
