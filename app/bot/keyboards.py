from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def moderation_keyboard(post_id: int, has_photos: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="Опубликовать", callback_data=f"mod:approve:{post_id}")]
    ]
    if has_photos:
        rows.append([InlineKeyboardButton(text="Без фото", callback_data=f"mod:nophoto:{post_id}")])
    rows.extend(
        [
            [InlineKeyboardButton(text="Редактировать", callback_data=f"mod:edit:{post_id}")],
            [InlineKeyboardButton(text="Отклонить", callback_data=f"mod:reject:{post_id}")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
