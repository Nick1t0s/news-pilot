from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    ReplyParameters,
)

from app.config import Settings
from app.photo.downloader import sniff_image_format
from app.textutil import plain_text

log = logging.getLogger("sender")


def parse_chat_id(value: str) -> str | int:
    value = value.strip()
    if value.lstrip("-").isdigit():
        return int(value)
    return value


class TelegramSender:
    """Publishing and moderation messaging via aiogram."""

    def __init__(self, bot: Bot, cfg: Settings) -> None:
        self._bot = bot
        self._cfg = cfg
        raw = cfg.telegram.channel_id.strip()
        self._channel = parse_chat_id(raw)
        self._public_username: str | None = raw.lstrip("@") if raw.startswith("@") else None
        self._private_prefix: str | None = None
        if self._public_username is None and str(self._channel).startswith("-100"):
            self._private_prefix = f"https://t.me/c/{str(self._channel)[4:]}"
        self._admin = cfg.telegram.admin_id

    def tg_url(self, message_id: int) -> str | None:
        if self._public_username:
            return f"https://t.me/{self._public_username}/{message_id}"
        if self._private_prefix:
            return f"{self._private_prefix}/{message_id}"
        return None

    async def send_to_channel(
        self, text: str, photos: list[tuple[str, bytes]], reply_to: int | None = None,
    ) -> tuple[int, str | None]:
        reply_parameters = ReplyParameters(message_id=reply_to) if reply_to else None
        photos = [(url, data) for url, data in photos if data]
        if photos:
            message_id = await self._send_with_photos(self._channel, text, photos, reply_parameters=reply_parameters)
        else:
            message = await self._send_text(self._channel, text, reply_parameters=reply_parameters)
            message_id = message.message_id
        return message_id, self.tg_url(message_id)

    async def send_moderation_draft(
        self, text: str, photos: list[tuple[str, bytes]], keyboard: InlineKeyboardMarkup,
    ) -> Message:
        photos = [(url, data) for url, data in photos if data]
        if photos:
            try:
                await self._send_with_photos(self._admin, "", photos)
            except Exception:
                log.exception("failed to send draft photos to admin")
        return await self._send_text(self._admin, text, keyboard=keyboard)

    async def send_admin_text(self, text: str) -> Message:
        return await self._send_text(self._admin, text)

    async def edit_message(self, chat_id, message_id: int, text: str) -> None:
        try:
            await self._call(
                self._bot.edit_message_text,
                chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML",
            )
        except TelegramBadRequest as exc:
            if "parse" not in str(exc).lower():
                raise
            await self._call(self._bot.edit_message_text, chat_id=chat_id, message_id=message_id, text=plain_text(text))

    async def _send_text(
        self, chat_id, text: str, keyboard: InlineKeyboardMarkup | None = None,
        reply_parameters: ReplyParameters | None = None,
    ) -> Message:
        try:
            return await self._call(
                self._bot.send_message, chat_id=chat_id, text=text, parse_mode="HTML",
                reply_markup=keyboard, reply_parameters=reply_parameters,
            )
        except TelegramBadRequest as exc:
            if "parse" not in str(exc).lower():
                raise
            return await self._call(
                self._bot.send_message, chat_id=chat_id, text=text,
                reply_markup=keyboard, reply_parameters=reply_parameters,
            )

    async def _send_with_photos(
        self, chat_id, caption: str, photos: list[tuple[str, bytes]],
        reply_parameters: ReplyParameters | None = None,
    ) -> int:
        files = [_input_file(data, index) for index, (_, data) in enumerate(photos)]
        if len(files) == 1:
            try:
                message = await self._call(
                    self._bot.send_photo,
                    chat_id=chat_id,
                    photo=files[0],
                    caption=caption or None,
                    parse_mode="HTML" if caption else None,
                    reply_parameters=reply_parameters,
                )
                return message.message_id
            except TelegramBadRequest as exc:
                if "parse" not in str(exc).lower():
                    raise
                message = await self._call(
                    self._bot.send_photo,
                    chat_id=chat_id,
                    photo=files[0],
                    caption=plain_text(caption) if caption else None,
                    reply_parameters=reply_parameters,
                )
                return message.message_id
        media = [
            InputMediaPhoto(
                media=file,
                caption=caption if index == 0 else None,
                parse_mode="HTML" if index == 0 and caption else None,
            )
            for index, file in enumerate(files)
        ]
        try:
            messages = await self._call(
                self._bot.send_media_group, chat_id=chat_id, media=media,
                reply_parameters=reply_parameters,
            )
            return messages[0].message_id
        except TelegramBadRequest as exc:
            if "parse" not in str(exc).lower():
                raise
            media[0].caption = plain_text(caption)
            media[0].parse_mode = None
            messages = await self._call(
                self._bot.send_media_group, chat_id=chat_id, media=media,
                reply_parameters=reply_parameters,
            )
            return messages[0].message_id


def _input_file(data: bytes, index: int) -> BufferedInputFile:
    fmt = sniff_image_format(data) or "jpg"
    return BufferedInputFile(data, filename=f"photo_{index}.{fmt}")


async def _call(self, method, **kwargs):
        delay = 1.0
        for attempt in range(4):
            try:
                return await method(**kwargs)
            except TelegramRetryAfter as exc:
                await asyncio.sleep(float(exc.retry_after) + 1.0)
            except (TelegramNetworkError, TimeoutError):
                if attempt == 3:
                    raise
                log.warning("telegram network error, retrying in %.1fs", delay)
                await asyncio.sleep(delay)
                delay *= 2
        raise RuntimeError("telegram retries exhausted")
