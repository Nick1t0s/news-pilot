from __future__ import annotations

from app.bot.keyboards import moderation_keyboard


def _callbacks(kb) -> list[str]:
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def test_keyboard_without_photos_has_no_nophoto_button() -> None:
    callbacks = _callbacks(moderation_keyboard(7))

    assert callbacks == ["mod:approve:7", "mod:edit:7", "mod:reject:7"]


def test_keyboard_with_photos_includes_nophoto_button() -> None:
    callbacks = _callbacks(moderation_keyboard(7, has_photos=True))

    assert callbacks == ["mod:approve:7", "mod:nophoto:7", "mod:edit:7", "mod:reject:7"]
