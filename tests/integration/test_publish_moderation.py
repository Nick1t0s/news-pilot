from __future__ import annotations

import datetime as dt

import pytest

from app.db import repo
from app.db.entities import DraftJob, PhotoRecord, PublishJob
from app.publish.service import PublishService
from tests.mocks import FakeEmbeddings, FakeSender

STUB_BYTES = b"\xff\xd8\xffstub-jpeg-data"


def make_job(
    *,
    source: str = "lenta",
    text: str = "Текст поста",
    source_url: str = "https://example.com/x",
    photos: list[PhotoRecord] | None = None,
    reply_to: int | None = None,
) -> PublishJob:
    return PublishJob(
        source=source,
        text=text,
        source_url=source_url,
        photos=list(photos or []),
        reply_to_message_id=reply_to,
        admin_note="",
    )


def make_service(settings, pool) -> tuple[PublishService, FakeSender]:
    sender = FakeSender()
    service = PublishService(settings, pool, sender, FakeEmbeddings(), http=None)
    return service, sender


async def test_publishes_without_photos_when_requested(settings, pool) -> None:
    service, sender = make_service(settings, pool)
    job = make_job(photos=[PhotoRecord(source_url="https://src/img.jpg", data=STUB_BYTES)])

    await service._publish(job, id(job), drop_photos=True)

    assert sender.published[-1]["photos"] == []
    assert sender.published[-1]["message_id"] > 0


async def test_default_publish_keeps_photos(settings, pool) -> None:
    service, sender = make_service(settings, pool)
    job = make_job(photos=[PhotoRecord(source_url="https://src/img.jpg", data=STUB_BYTES)])

    await service._publish(job, id(job))

    assert sender.published[-1]["photos"] == [("https://src/img.jpg", STUB_BYTES)]


async def test_missing_photo_bytes_are_refetched(settings, pool, monkeypatch) -> None:
    refetched: list[str] = []

    async def fake_download(http, url, *, timeout=20.0, retries=2):
        refetched.append(url)
        return STUB_BYTES

    monkeypatch.setattr("app.publish.service.download_image", fake_download)
    service, sender = make_service(settings, pool)
    job = make_job(photos=[PhotoRecord(source_url="https://src/img.jpg", data=None)])

    await service._publish(job, id(job))

    assert refetched == ["https://src/img.jpg"]
    assert sender.published[-1]["photos"] == [("https://src/img.jpg", STUB_BYTES)]


async def test_publishes_text_only_when_refetch_fails(settings, pool, monkeypatch) -> None:
    async def fake_download_fail(http, url, *, timeout=20.0, retries=2):
        return None

    monkeypatch.setattr("app.publish.service.download_image", fake_download_fail)
    service, sender = make_service(settings, pool)
    job = make_job(photos=[PhotoRecord(source_url="https://src/img.jpg", data=None)])

    await service._publish(job, id(job))

    assert sender.published[-1]["photos"] == []


async def test_reply_to_travels_inside_job(settings, pool) -> None:
    service, sender = make_service(settings, pool)
    job = make_job(reply_to=777)

    await service._publish(job, id(job))

    assert sender.published[-1]["reply_to"] == 777


async def test_auto_mode_notifies_admin_with_source(settings, pool) -> None:
    settings.publish.mode = "auto"
    settings.publish.notify_admin = True
    service, sender = make_service(settings, pool)
    job = make_job(source="lenta", source_url="https://example.com/x")

    await service._publish(job, id(job))

    assert len(sender.admin_texts) == 1
    assert "https://t.me/testchannel/" in sender.admin_texts[0]
    assert "lenta" in sender.admin_texts[0]


async def test_moderation_mode_does_not_notify_admin(settings, pool) -> None:
    settings.publish.mode = "moderation"
    settings.publish.notify_admin = True
    service, sender = make_service(settings, pool)
    job = make_job()
    service._drafts[1] = DraftJob(id=1, job=job, created_at=dt.datetime.now(dt.timezone.utc))

    await service._publish(job, 1)

    assert sender.admin_texts == []


async def test_append_source_adds_link_to_post(settings, pool) -> None:
    settings.publish.append_source = True
    service, sender = make_service(settings, pool)
    job = make_job(text="Текст поста", source_url="https://example.com/x")

    await service._publish(job, id(job))

    text = sender.published[-1]["text"]
    assert text.startswith("Текст поста")
    assert '<a href="https://example.com/x">Источник</a>' in text


async def test_append_source_disabled_keeps_text(settings, pool) -> None:
    service, sender = make_service(settings, pool)
    job = make_job(text="Текст поста")

    await service._publish(job, id(job))

    assert sender.published[-1]["text"] == "Текст поста"


async def test_published_post_is_stored_with_source(settings, pool) -> None:
    service, _ = make_service(settings, pool)
    job = make_job(source="ria", text="Текст поста про мост")

    await service._publish(job, id(job))

    posts = await repo.posts_by_source_since(pool, dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc))
    assert [(c.key, c.value) for c in posts] == [("ria", 1)]


async def test_moderation_flow_submit_approve(settings, pool) -> None:
    settings.publish.mode = "moderation"
    service, sender = make_service(settings, pool)

    await service.submit(make_job())
    assert service.drafts_count() == 1
    assert len(sender.drafts) == 1

    draft_id = next(iter(service._drafts))
    await service.approve_draft(draft_id)

    assert service.drafts_count() == 0
    assert len(sender.published) == 1
    assert service.get_draft(draft_id) is None


async def test_moderation_flow_edit_and_resend(settings, pool) -> None:
    settings.publish.mode = "moderation"
    service, sender = make_service(settings, pool)

    await service.submit(make_job())
    draft_id = next(iter(service._drafts))

    await service.apply_edit(draft_id, "Новый текст")
    assert service.get_draft(draft_id).job.text == "Новый текст"

    await service.reject_draft(draft_id, "rejected by admin")
    assert service.drafts_count() == 0
    assert sender.published == []


async def test_unknown_draft_lookup_errors(settings, pool) -> None:
    service, _ = make_service(settings, pool)

    with pytest.raises(LookupError):
        await service.approve_draft(999)


async def test_moderation_timeout_rejects_draft(settings, pool) -> None:
    settings.publish.mode = "moderation"
    settings.publish.moderation_timeout_hours = 0  # expires immediately
    service, sender = make_service(settings, pool)
    await service.submit(make_job())
    assert service.drafts_count() == 1

    expired = await service.expire_stale_drafts()

    assert expired == 1
    assert service.drafts_count() == 0
    assert await repo.counter_get(pool, repo.COUNTER_REJECTED) == 1
    assert any("таймауту" in text for text in sender.admin_texts)
