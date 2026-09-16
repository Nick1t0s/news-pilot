from __future__ import annotations

import logging

from aiogram import Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message

from app.bot.filters import AdminOnly
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
        pipeline = ctx.pipeline
        text = await build_stats_text(
            ctx.pool, ctx.cfg,
            queued_count=ctx.publisher.queued_count(),
            drafts_count=ctx.publisher.drafts_count(),
            processing_queued=pipeline.queued_count() if pipeline else 0,
            processing_active=pipeline.dedup_active() if pipeline else 0,
            selection_queued=pipeline.selection_queued() if pipeline else 0,
        )
        await message.answer(text)

    @router.callback_query(F.data.startswith("mod:approve:"))
    async def approve(callback: CallbackQuery, ctx) -> None:
        draft_id = _draft_id(callback)
        if draft_id is None:
            await callback.answer("Некорректные данные")
            return
        if ctx.publisher.get_draft(draft_id) is None:
            await callback.answer("Черновик утерян (перезапуск приложения)", show_alert=True)
            return
        await callback.answer("Публикую…")
        try:
            await ctx.publisher.approve_draft(draft_id)
        except Exception as exc:
            log.exception("manual publish failed: draft_id=%d", draft_id)
            await callback.answer(f"Ошибка публикации: {exc}", show_alert=True)
            return
        ref = ctx.publisher.pop_admin_message(draft_id)
        if ref is not None:
            chat_id, message_id = ref
            try:
                await ctx.sender.edit_message(chat_id, message_id, "✅ Опубликовано")
            except Exception:
                log.exception("failed to edit admin message after publish")

    @router.callback_query(F.data.startswith("mod:nophoto:"))
    async def approve_without_photos(callback: CallbackQuery, ctx) -> None:
        draft_id = _draft_id(callback)
        if draft_id is None:
            await callback.answer("Некорректные данные")
            return
        if ctx.publisher.get_draft(draft_id) is None:
            await callback.answer("Черновик утерян (перезапуск приложения)", show_alert=True)
            return
        await callback.answer("Публикую без фото…")
        try:
            await ctx.publisher.approve_draft(draft_id, drop_photos=True)
        except Exception as exc:
            log.exception("publish without photos failed: draft_id=%d", draft_id)
            await callback.answer(f"Ошибка публикации: {exc}", show_alert=True)
            return
        ref = ctx.publisher.pop_admin_message(draft_id)
        if ref is not None:
            chat_id, message_id = ref
            try:
                await ctx.sender.edit_message(chat_id, message_id, "✅ Опубликовано без фото")
            except Exception:
                log.exception("failed to edit admin message after publish without photos")

    @router.callback_query(F.data.startswith("mod:reject:"))
    async def reject(callback: CallbackQuery, ctx) -> None:
        draft_id = _draft_id(callback)
        if draft_id is None:
            await callback.answer("Некорректные данные")
            return
        await callback.answer("Отклонено")
        await ctx.publisher.reject_draft(draft_id, "rejected by admin")
        ref = ctx.publisher.pop_admin_message(draft_id)
        if ref is not None:
            chat_id, message_id = ref
            try:
                await ctx.sender.edit_message(chat_id, message_id, "🚫 Пост отклонён")
            except Exception as exc:  # noqa: BLE001
                log.warning("failed to edit admin message after reject: %s", exc)

    @router.callback_query(F.data.startswith("mod:edit:"))
    async def edit(callback: CallbackQuery, state: FSMContext, ctx) -> None:
        draft_id = _draft_id(callback)
        if draft_id is None:
            await callback.answer("Некорректные данные")
            return
        if ctx.publisher.get_draft(draft_id) is None:
            await callback.answer("Черновик утерян (перезапуск приложения)", show_alert=True)
            return
        await state.set_state(ModerationEdit.waiting_text)
        await state.update_data(draft_id=draft_id)
        await callback.answer()
        await callback.bot.send_message(
            cfg.telegram.admin_id,
            f"Пришли новый текст для черновика #{draft_id} (фото останутся прежними).",
        )

    @router.message(ModerationEdit.waiting_text, F.text)
    async def receive_edited_text(message: Message, state: FSMContext, ctx) -> None:
        data = await state.get_data()
        draft_id = data.get("draft_id")
        await state.clear()
        if draft_id is None:
            return
        text = sanitize_telegram_html(message.html_text or message.text or "")
        if not text.strip():
            await message.answer("Пустой текст, черновик не изменён")
            return
        await ctx.publisher.apply_edit(draft_id, text)
        try:
            draft = ctx.publisher.get_draft(draft_id)
            if draft is not None:
                await ctx.publisher.send_draft(draft.job)
        except Exception:
            log.exception("failed to re-send draft after edit: draft_id=%d", draft_id)
            await message.answer(f"Текст #{draft_id} обновлён, но черновик не переотправлен")
            return
        await message.answer(f"Текст #{draft_id} обновлён, черновик снова отправлен на модерацию")

    return router


def _draft_id(callback: CallbackQuery) -> int | None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or parts[0] != "mod":
        return None
    try:
        return int(parts[2])
    except ValueError:
        return None
