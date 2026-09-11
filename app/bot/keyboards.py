from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def moderation_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Опубликовать", callback_data=f"mod:approve:{post_id}")],
            [InlineKeyboardButton(text="Редактировать", callback_data=f"mod:edit:{post_id}")],
            [InlineKeyboardButton(text="Отклонить", callback_data=f"mod:reject:{post_id}")],
        ]
    )


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:refresh")],
        ]
    )
