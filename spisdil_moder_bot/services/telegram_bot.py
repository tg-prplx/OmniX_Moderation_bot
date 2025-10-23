from __future__ import annotations

import asyncio
import base64
import html
import shlex
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Optional
import re

import structlog
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..config import BotSettings
from ..models import ActionType, ChatContext, LayerType, MessageEnvelope, ModerationRule, RuleType
from ..punishments.aggregator import PunishmentDecision
from .moderation_service import ModerationCoordinator

logger = structlog.get_logger(__name__)

PANEL_HELP = (
    "🔧 *Панель администратора*\n"
    "• `list` — показать правила выбранного чата\n"
    "• `add <действие:время> <описание>` — добавить правило в чат (например, `mute:10m реклама`)\n"
    "• `add-global <действие:время> <описание>` — добавить глобальное правило\n"
    "• `remove <rule_id>` — удалить правило\n"
    "• `set <chat_id>` — переключиться на другой чат вручную\n"
    "• `help` — показать памятку\n"
    "• `cancel` — отменить текущий ввод\n"
    "\n"
    "_Можно пользоваться меню ниже или командами вручную._"
)


class TelegramModerationApp:
    """
    Aiogram integration wrapper that wires moderation pipeline into Telegram handlers.

    - Commands `/addrule`, `/removerule`, `/listrules` manage per-chat rule sets.
    - All text, captions, and photos are pushed into the moderation coordinator.
    - Decisions from the pipeline are mirrored into Telegram administrative actions.
    - `/panel` in DM opens an inline chat picker and text interface to manage rules.
    """

    def __init__(self, settings: BotSettings) -> None:
        self._settings = settings
        self.bot = Bot(token=settings.telegram_token)
        self.dispatcher = Dispatcher()
        self.coordinator = ModerationCoordinator(settings, decision_callback=self._on_decision)
        self._chat_cache: dict[int, str] = {}
        self._admin_sessions: dict[int, dict[str, Optional[int]]] = {}
        self._register_handlers()

    def _register_handlers(self) -> None:
        self.dispatcher.message(Command(commands=["start", "panel"]))(self._handle_panel_start)
        self.dispatcher.message(Command(commands=["help"]))(self._handle_help_command)
        self.dispatcher.message(Command(commands=["addrule"]))(self._handle_add_rule)
        self.dispatcher.message(Command(commands=["removerule"]))(self._handle_remove_rule)
        self.dispatcher.message(Command(commands=["listrules"]))(self._handle_list_rules)
        self.dispatcher.message(F.text | F.caption | F.photo)(self._handle_message)
        self.dispatcher.message(lambda msg: msg.chat.type == ChatType.PRIVATE)(self._handle_admin_text)
        self.dispatcher.callback_query(F.data.startswith("panel:chat:"))(self._handle_panel_select)
        self.dispatcher.callback_query(F.data.startswith("panel:action:"))(self._handle_panel_action)
        self.dispatcher.my_chat_member()(self._handle_my_chat_member)

    def _build_chat_selector_keyboard(self, admin_chats: list[tuple[int, str]]) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton(text="🌐 Global rules", callback_data="panel:chat:global")]
        ]
        for chat_id, title in admin_chats[:12]:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=title or str(chat_id),
                        callback_data=f"panel:chat:{chat_id}",
                    )
                ]
            )
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def _build_admin_menu(self, chat_key: str, *, include_global_shortcut: bool) -> InlineKeyboardMarkup:
        buttons: list[list[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(text="📋 Список правил", callback_data=f"panel:action:list:{chat_key}"),
                InlineKeyboardButton(text="🔄 Обновить", callback_data=f"panel:action:refresh:{chat_key}"),
            ],
            [
                InlineKeyboardButton(text="➕ Добавить правило", callback_data=f"panel:action:add:{chat_key}"),
                InlineKeyboardButton(text="➖ Удалить правило", callback_data=f"panel:action:remove:{chat_key}"),
            ],
        ]
        if include_global_shortcut:
            buttons.append(
                [InlineKeyboardButton(text="🌐 Перейти к глобальным", callback_data="panel:chat:global")]
            )
        buttons.append(
            [
                InlineKeyboardButton(text="🔁 Сменить чат", callback_data=f"panel:action:switch:{chat_key}"),
                InlineKeyboardButton(text="ℹ️ Помощь", callback_data=f"panel:action:help:{chat_key}"),
            ]
        )
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    async def _render_admin_panel(
        self,
        *,
        session: dict[str, Optional[int | str]],
        message: Optional[Message] = None,
        user_id: Optional[int] = None,
    ) -> None:
        chat_id = session.get("chat_id")
        chat_title = session.get("chat_title") or ("Global rules" if chat_id is None else str(chat_id))
        chat_key = "global" if chat_id is None else str(chat_id)
        status_line = session.get("last_status") or "Используйте кнопки ниже, чтобы управлять правилами."
        pending_action = session.get("pending_action")
        if pending_action == "add":
            status_line = "✏️ Отправьте новое правило в формате `<действие[:время]> <описание>` или `cancel`."
        elif pending_action == "add_global":
            status_line = "✏️ Отправьте глобальное правило в формате `<действие[:время]> <описание>` или `cancel`."
        elif pending_action == "remove":
            status_line = "✏️ Отправьте `rule_id`, который нужно удалить, или `cancel`."

        text = (
            f"*Управление чатом:* {chat_title}\n"
            f"`ID:` {chat_id if chat_id is not None else 'global'}\n\n"
            f"{status_line}\n\n"
            f"{PANEL_HELP}"
        )
        keyboard = self._build_admin_menu(chat_key, include_global_shortcut=chat_id is not None)
        if message is not None:
            rendered = await message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        elif user_id is not None and session.get("panel_message_id"):
            rendered = await self.bot.edit_message_text(
                text=text,
                chat_id=user_id,
                message_id=session["panel_message_id"],
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        else:
            rendered = await self.bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        session["panel_message_id"] = rendered.message_id

    async def _prompt_chat_selection(
        self,
        target_message: Message,
        user_id: int,
        *,
        replace: bool = False,
    ) -> None:
        admin_chats = await self._available_admin_chats(user_id)
        if not admin_chats:
            text = (
                "Пока что я не видел чатов, где вы админ.\n"
                "Добавьте бота в нужные группы и напишите там любое сообщение, "
                "либо отправьте `set <chat_id>` вручную."
            )
            if replace and target_message:
                await target_message.edit_text(text, parse_mode="Markdown")
            else:
                await target_message.answer(text, parse_mode="Markdown")
            return
        keyboard = self._build_chat_selector_keyboard(admin_chats)
        text = (
            "Выберите чат, которым хотите управлять.\n"
            "Можно использовать кнопки ниже или команду `set <chat_id>`."
        )
        if replace:
            rendered = await target_message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        else:
            rendered = await target_message.reply(text, parse_mode="Markdown", reply_markup=keyboard)
        session = self._admin_sessions.setdefault(user_id, {})
        session["pending_action"] = None
        session["panel_message_id"] = rendered.message_id

    def _format_rules_markdown(self, rules) -> str:
        if not rules:
            return "_Правила ещё не настроены._"
        lines = [
            f"• `{rule.rule_id}` — *{rule.layer.value}/{rule.rule_type.value}* — "
            f"{self._format_action_label(rule.action, rule.action_duration_seconds)}\n"
            f"  {rule.description}"
            for rule in rules
        ]
        return "*Активные правила:*\n" + "\n".join(lines)

    def _format_user_mention(self, ctx: ChatContext) -> str:
        if ctx.username:
            return f"@{html.escape(ctx.username)}"
        return f'<a href="tg://user?id={ctx.user_id}">{ctx.user_id}</a>'

    def _format_reason(self, reason: str) -> str:
        return html.escape(reason or "—")

    async def _handle_help_command(self, message: Message) -> None:
        if message.chat.type == ChatType.PRIVATE:
            await message.reply(PANEL_HELP, parse_mode="Markdown")
        else:
            await message.reply(
                "Commands:\n"
                "/addrule <action[:duration]> <scope:chat|global> <description>\n"
                "/removerule <rule_id>\n"
                "/listrules [chat|global]\n"
                "Open a private chat and send /panel for the full admin UI."
            )

    async def _handle_add_rule(self, message: Message) -> None:
        if message.chat.type == ChatType.PRIVATE:
            await message.reply("Use the admin panel to add rules. Type 'help' for instructions.")
            return
        self._remember_chat(message.chat)

        tokens = shlex.split(message.text or "")
        if len(tokens) < 4:
            await message.reply("Usage: /addrule <action[:duration]> <scope:chat|global> [layer=...] [type=...] [category=...] [pattern=...] <description>")
            return
        _, action_token, scope_token, *rest_tokens = tokens
        try:
            action, duration = self._parse_action_token(action_token)
        except ValueError as exc:
            await message.reply(str(exc))
            return

        try:
            layer_override, rule_type_override, category, pattern, description = self._extract_rule_metadata(rest_tokens)
        except ValueError as exc:
            await message.reply(str(exc))
            return

        if not description:
            await message.reply("Please provide rule description.")
            return

        chat_scope = scope_token.lower()
        if chat_scope not in {"chat", "global"}:
            await message.reply("Scope must be 'chat' or 'global'.")
            return
        chat_id = None if chat_scope == "global" else message.chat.id
        if chat_id is not None and not await self._ensure_admin(chat_id, message.from_user.id):
            await message.reply("You must be a chat admin to manage rules.")
            return
        try:
            rule = await self.coordinator.add_rule(
                description,
                action,
                chat_id=chat_id,
                action_duration_seconds=duration,
                layer=layer_override,
                rule_type=rule_type_override,
                category=category,
                pattern=pattern,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("add_rule_failed", error=str(exc))
            await message.reply("Failed to add rule. Check logs for details.")
            return
        scope_label = "global" if rule.chat_id is None else f"chat {rule.chat_id}"
        await message.reply(
            f"✅ Rule {rule.rule_id} added for {scope_label} via {rule.layer.value} layer."
        )

    async def _handle_remove_rule(self, message: Message) -> None:
        if message.chat.type != ChatType.PRIVATE:
            self._remember_chat(message.chat)
            if not await self._ensure_admin(message.chat.id, message.from_user.id):
                await message.reply("You must be a chat admin to manage rules.")
                return
        args = (message.text or "").split(maxsplit=1)
        if len(args) < 2:
            await message.reply("Usage: /removerule <rule_id>")
            return
        rule_id = args[1].strip()
        try:
            await self.coordinator.remove_rule(rule_id)
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("remove_rule_failed", error=str(exc))
            return
        await message.reply(f"🗑 Rule {rule_id} removed.")

    async def _handle_list_rules(self, message: Message) -> None:
        if message.chat.type != ChatType.PRIVATE:
            self._remember_chat(message.chat)
        args = (message.text or "").split(maxsplit=1)
        scope = "chat"
        if len(args) > 1:
            scope = args[1].strip().lower()
        chat_id = None if scope == "global" else message.chat.id
        if chat_id is not None and message.chat.type != ChatType.PRIVATE:
            if not await self._ensure_admin(chat_id, message.from_user.id):
                await message.reply("You must be a chat admin to view protected rules.")
                return
        rules = await self.coordinator.list_rules(chat_id)
        text = self._format_rules_markdown(rules)
        await message.reply(text, parse_mode="Markdown")
    async def _on_decision(self, decision: PunishmentDecision, result) -> None:
        verdict = decision.verdict
        ctx = result.message.context
        duration_seconds = verdict.details.get("action_duration_seconds")
        logger.info(
            "telegram_decision",
            chat_id=ctx.chat_id,
            user_id=ctx.user_id,
            action=verdict.action.value,
            rule=verdict.rule_code,
            duration=duration_seconds,
        )
        user_ref = self._format_user_mention(ctx)
        rule_ref = html.escape(verdict.rule_code)
        reason_html = self._format_reason(verdict.reason)
        try:
            if verdict.action == ActionType.DELETE:
                await self.bot.delete_message(ctx.chat_id, ctx.message_id)
            elif verdict.action == ActionType.WARN:
                text = (
                    "⚠️ <b>Предупреждение</b>\n"
                    f"Пользователь: {user_ref}\n"
                    f"Причина: {reason_html}\n"
                    f"Правило: <code>{rule_ref}</code>"
                )
                await self.bot.send_message(
                    ctx.chat_id,
                    text,
                    parse_mode="HTML",
                    reply_to_message_id=ctx.message_id,
                )
            elif verdict.action == ActionType.MUTE:
                seconds = duration_seconds or 15 * 60
                until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
                await self.bot.restrict_chat_member(
                    ctx.chat_id,
                    ctx.user_id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=until,
                )
                await self.bot.send_message(
                    ctx.chat_id,
                    "🔇 <b>Мут</b>\n"
                    f"Пользователь: {user_ref}\n"
                    f"Длительность: {html.escape(self._humanize_duration(seconds))}\n"
                    f"Причина: {reason_html}\n"
                    f"Правило: <code>{rule_ref}</code>",
                    parse_mode="HTML",
                )
            elif verdict.action == ActionType.BAN:
                if duration_seconds:
                    until = datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)
                    await self.bot.ban_chat_member(ctx.chat_id, ctx.user_id, until_date=until)
                    await self.bot.send_message(
                        ctx.chat_id,
                        "🚫 <b>Бан</b>\n"
                        f"Пользователь: {user_ref}\n"
                        f"Длительность: {html.escape(self._humanize_duration(duration_seconds))}\n"
                        f"Причина: {reason_html}\n"
                        f"Правило: <code>{rule_ref}</code>",
                        parse_mode="HTML",
                    )
                else:
                    await self.bot.ban_chat_member(ctx.chat_id, ctx.user_id)
                    await self.bot.send_message(
                        ctx.chat_id,
                        "🚫 <b>Бан</b>\n"
                        f"Пользователь: {user_ref}\n"
                        f"Причина: {reason_html}\n"
                        f"Правило: <code>{rule_ref}</code>",
                        parse_mode="HTML",
                    )
        except Exception as exc:  # pragma: no cover - network errors
            logger.error(
                "telegram_decision_error",
                error=str(exc),
                action=verdict.action.value,
                chat_id=ctx.chat_id,
            )

    async def _handle_message(self, message: Message) -> None:
        if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP, ChatType.PRIVATE}:
            return
        if message.text and message.text.startswith('/'):
            return  # commands handled separately
        if message.chat.type != ChatType.PRIVATE:
            self._remember_chat(message.chat)
        else:
            command_text = (message.text or message.caption or '').strip().lower()
            admin_prefixes = ("set", "add", "add-global", "remove", "list", "help")
            if any(command_text.startswith(prefix) for prefix in admin_prefixes):
                return

        images = await self._collect_images(message)
        envelope = MessageEnvelope(
            context=ChatContext(
                chat_id=message.chat.id,
                user_id=message.from_user.id if message.from_user else 0,
                message_id=message.message_id,
                timestamp=message.date.replace(tzinfo=timezone.utc),
                username=message.from_user.username if message.from_user else None,
                language_code=message.from_user.language_code if message.from_user else None,
            ),
            text=message.text,
            caption=message.caption,
            media_type=self._detect_media_type(message),
            images=images,
            metadata={
                "telegram_content_type": message.content_type,
                "chat_title": message.chat.title,
            },
        )
        logger.info(
            "telegram_message_ingested",
            chat_id=envelope.context.chat_id,
            message_id=envelope.context.message_id,
            media_type=envelope.media_type,
            images=len(images),
        )
        await self.coordinator.ingest(envelope)

    async def _collect_images(self, message: Message) -> list[str]:
        if not message.photo:
            return []
        largest_photo = message.photo[-1]
        file = await self.bot.get_file(largest_photo.file_id)
        buffer = BytesIO()
        await self.bot.download(file, destination=buffer)
        mime = "image/jpeg"
        if file.file_path and file.file_path.lower().endswith(".png"):
            mime = "image/png"
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return [f"data:{mime};base64,{encoded}"]

    def _detect_media_type(self, message: Message) -> Optional[str]:
        if message.photo:
            return "photo"
        if message.document and message.document.mime_type:
            return message.document.mime_type
        return None
    async def run(self) -> None:
        await self.coordinator.start()
        try:
            await self.dispatcher.start_polling(self.bot)
        finally:
            await self.coordinator.shutdown()
            await self.bot.session.close()

    async def _handle_panel_start(self, message: Message) -> None:
        if message.chat.type != ChatType.PRIVATE:
            await message.reply("Open a private chat with me and send /panel to manage moderation.")
            return
        self._admin_sessions.pop(message.from_user.id, None)
        await self._prompt_chat_selection(message, message.from_user.id, replace=False)

    async def _handle_panel_select(self, callback: CallbackQuery) -> None:
        await callback.answer()
        raw_chat_id = callback.data.split(":")[2]
        user_id = callback.from_user.id
        admin_chats = dict(await self._available_admin_chats(user_id))
        if raw_chat_id == "global":
            chat_id = None
            chat_title = "Глобальные правила"
        else:
            try:
                chat_id = int(raw_chat_id)
            except ValueError:
                await callback.message.edit_text("Некорректный идентификатор чата.")
                return
            if chat_id not in admin_chats:
                await callback.message.edit_text("Вы не администратор в этом чате или он недоступен.")
                return
            chat_title = admin_chats[chat_id]
        session = self._admin_sessions.setdefault(user_id, {})
        session.update(
            {
                "chat_id": chat_id,
                "chat_title": chat_title,
                "pending_action": None,
                "last_status": "Используйте кнопки ниже, чтобы управлять правилами.",
            }
        )
        await self._render_admin_panel(session=session, message=callback.message, user_id=user_id)

    async def _handle_panel_action(self, callback: CallbackQuery) -> None:
        await callback.answer()
        parts = callback.data.split(":", 3)
        if len(parts) < 4:
            return
        _, _, action, chat_key = parts
        user_id = callback.from_user.id
        session = self._admin_sessions.get(user_id)
        if not session:
            await callback.message.answer("Сессия устарела. Отправьте /panel ещё раз.")
            return
        expected_key = "global" if session.get("chat_id") is None else str(session.get("chat_id"))
        if chat_key != expected_key:
            await self._render_admin_panel(session=session, message=callback.message, user_id=user_id)
            return

        chat_id = session.get("chat_id")
        if action == "list":
            rules = await self.coordinator.list_rules(chat_id)
            await callback.message.answer(self._format_rules_markdown(rules), parse_mode="Markdown")
            session["last_status"] = "📋 Отправил актуальный список правил ниже."
        elif action == "refresh":
            session["last_status"] = "🔄 Панель обновлена."
        elif action == "add":
            if chat_id is None:
                session["pending_action"] = "add_global"
                prompt = (
                    "Отправьте глобальное правило в формате `warn:1h описание` (время необязательно). "
                    "Пример: `ban продажа наркотиков`. Напишите `cancel`, чтобы отменить."
                )
            else:
                session["pending_action"] = "add"
                prompt = (
                    "Отправьте новое правило в формате `warn:10m описание` (время необязательно). "
                    "Можно добавлять `category=...` или `layer=...`. Напишите `cancel`, чтобы отменить."
                )
            session["last_status"] = None
            await callback.message.answer(prompt, parse_mode="Markdown")
        elif action == "remove":
            session["pending_action"] = "remove"
            session["last_status"] = None
            await callback.message.answer(
                "Отправьте `rule_id`, который нужно удалить. Напишите `cancel`, чтобы отменить.",
                parse_mode="Markdown",
            )
        elif action == "help":
            await callback.message.answer(PANEL_HELP, parse_mode="Markdown")
            session["last_status"] = "ℹ️ Отправил памятку ниже."
        elif action == "switch":
            session["pending_action"] = None
            session["last_status"] = "Выберите чат из списка."
            await self._prompt_chat_selection(callback.message, user_id, replace=True)
            return
        else:
            session["last_status"] = "Неизвестное действие."
        await self._render_admin_panel(session=session, message=callback.message, user_id=user_id)

    async def _handle_admin_text(self, message: Message) -> None:
        if message.text and message.text.startswith("/"):
            return  # slash commands handled separately
        user_id = message.from_user.id
        session = self._admin_sessions.get(user_id)
        if not session or "chat_id" not in session:
            await message.answer("Send /panel to choose a chat to manage.")
            return

        text = (message.text or message.caption or "").strip()
        if not text:
            await message.answer(PANEL_HELP, parse_mode="Markdown")
            return

        pending = session.get("pending_action")
        if pending:
            lower = text.lower()
            if lower == "cancel":
                session["pending_action"] = None
                session["last_status"] = "✋ Действие отменено."
                await message.answer("Отменено. Выберите следующее действие в панели.")
                await self._render_admin_panel(session=session, user_id=user_id)
                return
            if pending == "add":
                rule = await self._admin_add_rule(message, session.get("chat_id"), command=f"add {text}")
                session["last_status"] = (
                    f"✅ Добавлено правило `{rule.rule_id}`." if rule else "⚠️ Не удалось добавить правило."
                )
            elif pending == "add_global":
                rule = await self._admin_add_rule(message, chat_id=None, command=f"add-global {text}")
                session["last_status"] = (
                    f"✅ Добавлено глобальное правило `{rule.rule_id}`." if rule else "⚠️ Не удалось добавить правило."
                )
            elif pending == "remove":
                rule_id = text
                if lower.startswith("remove"):
                    parts = text.split(maxsplit=1)
                    if len(parts) < 2:
                        await message.answer("Укажите `rule_id` после команды или отправьте `cancel`.")
                        return
                    rule_id = parts[1].strip()
                try:
                    await self.coordinator.remove_rule(rule_id)
                    await message.answer(f"Removed rule {rule_id}")
                    session["last_status"] = f"🗑 Удалено правило `{rule_id}`."
                except Exception as exc:  # pylint: disable=broad-except
                    logger.error("remove_rule_failed", error=str(exc))
                    await message.answer("Failed to remove rule. Check logs.")
                    session["last_status"] = "⚠️ Не удалось удалить правило."
            session["pending_action"] = None
            await self._render_admin_panel(session=session, user_id=user_id)
            return

        lower = text.lower()
        chat_id = session.get("chat_id")
        if lower == "help":
            await message.answer(PANEL_HELP, parse_mode="Markdown")
            session["last_status"] = "ℹ️ Памятка отправлена."
            await self._render_admin_panel(session=session, user_id=user_id)
            return
        if lower == "list":
            rules = await self.coordinator.list_rules(chat_id)
            await message.answer(self._format_rules_markdown(rules), parse_mode="Markdown")
            session["last_status"] = "📋 Список правил отправлен ниже."
            await self._render_admin_panel(session=session, user_id=user_id)
            return
        if lower.startswith("remove"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                await message.answer("Usage: remove <rule_id>")
                return
            if chat_id is not None and not await self._ensure_admin(chat_id, user_id):
                await message.answer("You are not an admin in that chat.")
                return
            try:
                await self.coordinator.remove_rule(parts[1])
                await message.answer(f"Removed rule {parts[1]}")
                session["last_status"] = f"🗑 Удалено правило `{parts[1]}`."
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("remove_rule_failed", error=str(exc))
                await message.answer("Failed to remove rule. Check logs.")
                session["last_status"] = "⚠️ Не удалось удалить правило."
            await self._render_admin_panel(session=session, user_id=user_id)
            return
        if lower.startswith("set"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                await message.answer("Usage: set <chat_id|global>")
                return
            target = parts[1].strip().lower()
            if target == "global":
                session.update({"chat_id": None, "chat_title": "Глобальные правила", "pending_action": None})
                await message.answer("Switched to global rules. Type `list` to see rules.", parse_mode="Markdown")
                await self._render_admin_panel(session=session, user_id=user_id)
                return
            try:
                new_chat_id = int(target)
            except ValueError:
                await message.answer("Chat ID must be an integer or `global`.")
                return
            if not await self._ensure_admin(new_chat_id, user_id):
                await message.answer("You are not an admin in that chat.")
                return
            session.update(
                {
                    "chat_id": new_chat_id,
                    "chat_title": self._chat_cache.get(new_chat_id, str(new_chat_id)),
                    "pending_action": None,
                }
            )
            await message.answer(f"Switched to chat {new_chat_id}. Type `list` to see rules.")
            await self._render_admin_panel(session=session, user_id=user_id)
            return
        if lower.startswith("add-global"):
            rule = await self._admin_add_rule(message, chat_id=None, command=text)
            session["last_status"] = (
                f"✅ Добавлено глобальное правило `{rule.rule_id}`." if rule else "⚠️ Не удалось добавить правило."
            )
            await self._render_admin_panel(session=session, user_id=user_id)
            return
        if lower.startswith("add"):
            if chat_id is None:
                await message.answer("Выберите чат или используйте `add-global` для глобального правила.")
                return
            rule = await self._admin_add_rule(message, chat_id=chat_id, command=text)
            session["last_status"] = (
                f"✅ Добавлено правило `{rule.rule_id}`." if rule else "⚠️ Не удалось добавить правило."
            )
            await self._render_admin_panel(session=session, user_id=user_id)
            return
        await message.answer("Unknown command. Type 'help' for instructions.")

    async def _admin_add_rule(self, message: Message, chat_id: Optional[int], command: str) -> Optional[ModerationRule]:
        tokens = shlex.split(command)
        if len(tokens) < 3:
            await message.answer("Usage: add <action[:duration]> [layer=...] [type=...] [category=...] <description>")
            return None
        _, action_token, *rest_tokens = tokens
        try:
            action, duration = self._parse_action_token(action_token)
        except ValueError as exc:
            await message.answer(str(exc))
            return None

        try:
            layer_override, rule_type_override, category, pattern, description = self._extract_rule_metadata(rest_tokens)
        except ValueError as exc:
            await message.answer(str(exc))
            return None

        if duration is None and description:
            first_word = description.split(maxsplit=1)[0]
            if self._looks_like_duration(first_word):
                try:
                    duration = self._parse_duration(first_word)
                except ValueError as exc:
                    await message.answer(str(exc))
                    return None
                description = description.split(maxsplit=1)[1] if ' ' in description else ''
        if not description:
            await message.answer("Please provide rule description.")
            return None
        if chat_id is not None and not await self._ensure_admin(chat_id, message.from_user.id):
            await message.answer("You are not an admin in that chat.")
            return None
        try:
            rule = await self.coordinator.add_rule(
                description,
                action,
                chat_id=chat_id,
                action_duration_seconds=duration,
                layer=layer_override,
                rule_type=rule_type_override,
                category=category,
                pattern=pattern,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("panel_add_rule_failed", error=str(exc))
            await message.answer("Failed to add rule. Check logs for details.")
            return None
        scope_label = "global" if chat_id is None else f"chat {chat_id}"
        await message.answer(f"Rule {rule.rule_id} added for {scope_label}.")
        return rule

    async def _available_admin_chats(self, user_id: int) -> list[tuple[int, str]]:
        chats = []
        for chat_id, title in self._chat_cache.items():
            try:
                admins = await self.bot.get_chat_administrators(chat_id)
            except (TelegramForbiddenError, TelegramBadRequest) as exc:
                logger.warning("admin_check_failed", chat_id=chat_id, error=str(exc))
                continue
            if any(admin.user.id == user_id for admin in admins):
                chats.append((chat_id, title))
        return chats

    async def _handle_my_chat_member(self, update: ChatMemberUpdated) -> None:
        chat = update.chat
        status = update.new_chat_member.status
        if status in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}:
            self._remember_chat(chat)
        elif status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
            self._chat_cache.pop(chat.id, None)

    def _remember_chat(self, chat) -> None:
        if chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
            title = chat.title or getattr(chat, "full_name", "") or str(chat.id)
            self._chat_cache[chat.id] = title

    def _extract_rule_metadata(
        self,
        tokens: list[str],
    ) -> tuple[Optional[LayerType], Optional[RuleType], Optional[str], Optional[str], str]:
        layer_override: Optional[LayerType] = None
        rule_type_override: Optional[RuleType] = None
        category: Optional[str] = None
        pattern: Optional[str] = None
        description_tokens: list[str] = []
        for token in tokens:
            lower = token.lower()
            if not description_tokens:
                if lower.startswith("layer="):
                    layer_override = self._parse_layer_value(token.split("=", 1)[1])
                    continue
                if lower.startswith("type="):
                    rule_type_override = self._parse_rule_type_value(token.split("=", 1)[1])
                    continue
                if lower.startswith("category="):
                    category = token.split("=", 1)[1]
                    continue
                if lower.startswith("pattern="):
                    pattern = token.split("=", 1)[1]
                    continue
            description_tokens.append(token)
        description = " ".join(description_tokens)
        return layer_override, rule_type_override, category, pattern, description

    def _parse_action_token(self, token: str) -> tuple[ActionType, Optional[int]]:
        raw = token.strip()
        if raw.startswith("/"):
            raw = raw.lstrip("/")
        normalized = raw.replace("[", "").replace("]", "")
        base = normalized
        duration = None
        if ":" in normalized:
            base, duration_part = normalized.split(":", 1)
            duration_part = duration_part.strip()
            if not duration_part:
                raise ValueError("Duration must follow the action, e.g. mute:10m")
            # allow formats like "10m текст" by taking first token as duration
            duration_token = duration_part.split()[0]
            duration = self._parse_duration(duration_token)
        try:
            action = ActionType(base.lower())
        except ValueError as exc:
            raise ValueError("Unknown action. Use delete|warn|mute|ban.") from exc
        return action, duration

    def _looks_like_duration(self, token: str) -> bool:
        try:
            self._parse_duration(token)
            return True
        except ValueError:
            return False

    def _parse_layer_value(self, value: str) -> LayerType:
        normalized = value.lower()
        mapping = {
            "regex": LayerType.REGEX,
            "omni": LayerType.OMNI,
            "chatgpt": LayerType.CHATGPT,
            "gpt": LayerType.CHATGPT,
        }
        if normalized not in mapping:
            raise ValueError("Unknown layer. Use regex|omni|gpt.")
        return mapping[normalized]

    def _parse_rule_type_value(self, value: str) -> RuleType:
        normalized = value.lower()
        mapping = {
            "regex": RuleType.REGEX,
            "semantic": RuleType.SEMANTIC,
            "contextual": RuleType.CONTEXTUAL,
        }
        if normalized not in mapping:
            raise ValueError("Unknown rule type. Use regex|semantic|contextual.")
        return mapping[normalized]

    def _parse_duration(self, token: str) -> int:
        token = token.lower()
        pattern = re.compile(r"(\d+)([smhd])")
        matches = list(pattern.finditer(token))
        if not matches:
            raise ValueError("Invalid duration format. Use values like 30s, 10m, 2h, 3d.")
        total = 0
        consumed = 0
        for match in matches:
            start, end = match.span()
            if start != consumed:
                raise ValueError("Invalid duration format. Use values like 30s, 10m, 2h, 3d.")
            consumed = end
            value = int(match.group(1))
            unit = match.group(2)
            if unit == "s":
                total += value
            elif unit == "m":
                total += value * 60
            elif unit == "h":
                total += value * 3600
            elif unit == "d":
                total += value * 86400
        if consumed != len(token):
            raise ValueError("Invalid duration format. Use values like 30s, 10m, 2h, 3d.")
        return total

    def _format_action_label(self, action: ActionType, duration_seconds: Optional[int]) -> str:
        if duration_seconds:
            return f"{action.value} ({self._humanize_duration(duration_seconds)})"
        return action.value

    def _humanize_duration(self, seconds: int) -> str:
        parts = []
        remaining = seconds
        for unit_seconds, suffix in ((86400, "d"), (3600, "h"), (60, "m"), (1, "s")):
            if remaining >= unit_seconds:
                value = remaining // unit_seconds
                remaining %= unit_seconds
                parts.append(f"{value}{suffix}")
        return " ".join(parts) if parts else "0s"
    async def _ensure_admin(self, chat_id: int, user_id: int) -> bool:
        try:
            member = await self.bot.get_chat_member(chat_id, user_id)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.warning("admin_check_failed", chat_id=chat_id, user_id=user_id, error=str(exc))
            return False
        return member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}


@asynccontextmanager
async def telegram_app(settings: BotSettings):
    app = TelegramModerationApp(settings)
    try:
        yield app
    finally:
        await app.coordinator.shutdown()
        await app.bot.session.close()
