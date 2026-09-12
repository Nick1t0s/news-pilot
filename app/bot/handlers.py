from __future__ import annotations

import logging

from aiogram import Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message

from app.bot.filters import AdminOnly
from app.bot.keyboards import admin_menu_keyboard
from app.bot.stats import build_stats_text
from app.config import Settings
from app.textutil import sanitize_telegram_html

log = logging.getLogger("bot")


class ModerationEdit(StatesGroup):
    waiting_text = State()


def build_dispatcher(cfg: Settings) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(_admin_router(cfg))
    return dp


def _admin_router(cfg: Settings) -> Router:
    router = Router(name="admin")
    admin_filter = AdminOnly(cfg.telegram.admin_id)
    router.message.filter(admin_filter)
    router.callback_query.filter(admin_filter)

    @router.message(Command("admin"))
    async def admin_menu(message: Message, ctx) -> None:
        text = await build_stats_text(ctx.pool, ctx.cfg, ctx.publisher.queued_count())
        await message.answer(text, reply_markup=admin_menu_keyboard())

    @router.callback_query(F.data.in_({"admin:stats", "admin:refresh"}))
    async def admin_refresh(callback: CallbackQuery, ctx) -> None:
        await callback.answer()
        if callback.message is None:
            return
        text = await build_stats_text(ctx.pool, ctx.cfg, ctx.publisher.queued_count())
        try:
            await callback.message.edit_text(text, reply_markup=admin_menu_keyboard())
        except Exception as exc:  # noqa: BLE001
            log.warning("stats refresh failed: %s", exc)

    @router.callback_query(F.data.startswith("mod:approve:"))
    async def approve(callback: CallbackQuery, ctx) -> None:
        post_id = _post_id(callback)
        if post_id is None:
            await callback.answer("Некорректные данные")
            return
        await callback.answer("Публикую…")
        try:
            post = await ctx.publisher.approve(post_id)
        except Exception as exc:
            log.exception("manual publish failed: post_id=%s", post_id)
            await callback.answer(f"Ошибка публикации: {exc}", show_alert=True)
            return
        ref = ctx.publisher.pop_admin_message(post_id)
        if ref is not None:
            chat_id, message_id = ref
            link = post.tg_url or f"id {post.tg_message_id}"
            try:
                await ctx.sender.edit_message(chat_id, message_id, f"✅ Опубликовано: {link}")
            except Exception:
                log.exception("failed to edit admin message after publish")

    @router.callback_query(F.data.startswith("mod:nophoto:"))
    async def approve_without_photos(callback: CallbackQuery, ctx) -> None:
        post_id = _post_id(callback)
        if post_id is None:
            await callback.answer("Некорректные данные")
            return
        await callback.answer("Публикую без фото…")
        try:
            post = await ctx.publisher.approve(post_id, drop_photos=True)
        except Exception as exc:
            log.exception("publish without photos failed: post_id=%s", post_id)
            await callback.answer(f"Ошибка публикации: {exc}", show_alert=True)
            return
        ref = ctx.publisher.pop_admin_message(post_id)
        if ref is not None:
            chat_id, message_id = ref
            link = post.tg_url or f"id {post.tg_message_id}"
            try:
                await ctx.sender.edit_message(chat_id, message_id, f"✅ Опубликовано без фото: {link}")
            except Exception:
                log.exception("failed to edit admin message after publish without photos")

    @router.callback_query(F.data.startswith("mod:reject:"))
    async def reject(callback: CallbackQuery, ctx) -> None:
        post_id = _post_id(callback)
        if post_id is None:
            await callback.answer("Некорректные данные")
            return
        await callback.answer("Отклонено")
        await ctx.publisher.reject(post_id, "rejected by admin")
        ref = ctx.publisher.pop_admin_message(post_id)
        if ref is not None:
            chat_id, message_id = ref
            try:
                await ctx.sender.edit_message(chat_id, message_id, "🚫 Пост отклонён")
            except Exception as exc:  # noqa: BLE001
                log.warning("failed to edit admin message after reject: %s", exc)

    @router.callback_query(F.data.startswith("mod:edit:"))
    async def edit(callback: CallbackQuery, state: FSMContext, ctx) -> None:
        post_id = _post_id(callback)
        if post_id is None:
            await callback.answer("Некорректные данные")
            return
        await state.set_state(ModerationEdit.waiting_text)
        await state.update_data(post_id=post_id)
        await callback.answer()
        await callback.bot.send_message(
            cfg.telegram.admin_id,
            f"Пришли новый текст для черновика #{post_id} (фото останутся прежними).",
        )

    @router.message(ModerationEdit.waiting_text, F.text)
    async def receive_edited_text(message: Message, state: FSMContext, ctx) -> None:
        data = await state.get_data()
        post_id = data.get("post_id")
        await state.clear()
        if post_id is None:
            return
        text = sanitize_telegram_html(message.html_text or message.text or "")
        if not text.strip():
            await message.answer("Пустой текст, черновик не изменён")
            return
        await ctx.publisher.apply_edit(post_id, text)
        try:
            await ctx.publisher.send_draft(post_id)
        except Exception:
            log.exception("failed to re-send draft after edit: post_id=%s", post_id)
            await message.answer(f"Текст #{post_id} обновлён, но черновик не переотправлен")
            return
        await message.answer(f"Текст #{post_id} обновлён, черновик снова отправлен на модерацию")

    return router


def _post_id(callback: CallbackQuery) -> int | None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or parts[0] != "mod":
        return None
    try:
        return int(parts[2])
    except ValueError:
        return None
