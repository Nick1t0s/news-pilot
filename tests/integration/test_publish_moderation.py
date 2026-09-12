from __future__ import annotations

from app.db import repo
from app.db.entities import NewsStatus, PostStatus
from app.publish.service import PublishService
from tests.mocks import FakeEmbeddings, FakeSender


async def _make_post(pool, *, external_id: str, photo: str | None) -> int:
    news_id = await repo.add_news(
        pool,
        source="lenta",
        external_id=external_id,
        title="Новость",
        text="Текст новости про мост",
        url="https://example.com/x",
        full_text_fetched=True,
        published_at=None,
        status=NewsStatus.published,
    )
    post_id = await repo.insert_post(pool, news_id, "Текст поста", PostStatus.draft)
    if photo is not None:
        await repo.add_post_images(pool, post_id, [("https://src/img.jpg", photo)])
    return post_id


def make_service(settings, pool) -> tuple[PublishService, FakeSender]:
    sender = FakeSender()
    return PublishService(settings, pool, sender, FakeEmbeddings()), sender


async def test_publishes_without_photos_when_requested(settings, pool) -> None:
    service, sender = make_service(settings, pool)
    post_id = await _make_post(pool, external_id="nophoto-1", photo="data/images/x.jpg")

    post = await service.approve(post_id, drop_photos=True)

    assert sender.published[-1]["photos"] == []
    assert post.status == PostStatus.published
    assert post.tg_message_id == sender.published[-1]["message_id"]
    news = await repo.get_news(pool, (await repo.get_post(pool, post_id)).news_id)
    assert news.status == NewsStatus.published


async def test_default_publish_keeps_photos(settings, pool) -> None:
    service, sender = make_service(settings, pool)
    post_id = await _make_post(pool, external_id="withphoto-1", photo="data/images/y.jpg")

    await service.approve(post_id)

    assert sender.published[-1]["photos"] == ["data/images/y.jpg"]
    post = await repo.get_post(pool, post_id)
    assert post.status == PostStatus.published


async def test_restore_in_auto_mode_approves_drafts(settings, pool) -> None:
    settings.publish.mode = "auto"
    service, sender = make_service(settings, pool)
    post_id = await _make_post(pool, external_id="restore-1", photo=None)

    await service.restore()

    post = await repo.get_post(pool, post_id)
    assert post.status == PostStatus.published
    assert len(sender.published) == 1
    assert sender.drafts == []
