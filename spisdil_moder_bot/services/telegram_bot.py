from __future__ import annotations

import asyncio
import base64
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
from ..models import ActionType, ChatContext, LayerType, MessageEnvelope, RuleType
from ..punishments.aggregator import PunishmentDecision
from .moderation_service import ModerationCoordinator

logger = structlog.get_logger(__name__)

PANEL_HELP = (
    "🔧 *Admin Panel Commands*\n"
    "`list` – show rules\n"
    "`add <action[:duration]> <description>` – create chat rule (e.g. `add mute:10m реклама` "
    "or `add mute 10m реклама`)\n"
    "`add-global <action[:duration]> <description>` – create global rule\n"
    "`remove <rule_id>` – delete rule\n"
    "`set <chat_id>` – manually switch chat\n"
    "`help` – show this message"
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
        self.dispatcher.my_chat_member()(self._handle_my_chat_member)

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
        if not rules:
            await message.reply("No rules configured yet.")
            return
        lines = [
            f"{rule.rule_id} [{rule.layer.value}|{rule.rule_type.value}] "
            f"action={self._format_action_label(rule.action, rule.action_duration_seconds)} "
            f"chat={rule.chat_id or 'global'} – {rule.description}"
            for rule in rules
        ]
        await message.reply("📋 Active rules:\n" + "\n".join(lines))
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
        try:
            if verdict.action == ActionType.DELETE:
                await self.bot.delete_message(ctx.chat_id, ctx.message_id)
            elif verdict.action == ActionType.WARN:
                await self.bot.send_message(
                    ctx.chat_id,
                    f"⚠️ Warning for @{ctx.username or ctx.user_id}: {verdict.reason}",
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
                    f"🔇 User @{ctx.username or ctx.user_id} muted for {self._humanize_duration(seconds)}: {verdict.reason}",
                )
            elif verdict.action == ActionType.BAN:
                if duration_seconds:
                    until = datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)
                    await self.bot.ban_chat_member(ctx.chat_id, ctx.user_id, until_date=until)
                    await self.bot.send_message(
                        ctx.chat_id,
                        f"🚫 User @{ctx.username or ctx.user_id} banned for {self._humanize_duration(duration_seconds)}: {verdict.reason}",
                    )
                else:
                    await self.bot.ban_chat_member(ctx.chat_id, ctx.user_id)
                    await self.bot.send_message(
                        ctx.chat_id,
                        f"🚫 User @{ctx.username or ctx.user_id} banned: {verdict.reason}",
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

    async def _on_decision(self, decision: PunishmentDecision, result) -> None:
        verdict = decision.verdict
        ctx = result.message.context
        logger.info(
            "telegram_decision",
            chat_id=ctx.chat_id,
            user_id=ctx.user_id,
            action=verdict.action.value,
            rule=verdict.rule_code,
        )
        try:
            if verdict.action == ActionType.DELETE:
                await self.bot.delete_message(ctx.chat_id, ctx.message_id)
            elif verdict.action == ActionType.WARN:
                await self.bot.send_message(
                    ctx.chat_id,
                    f"⚠️ Warning for @{ctx.username or ctx.user_id}: {verdict.reason}",
                    reply_to_message_id=ctx.message_id,
                )
            elif verdict.action == ActionType.MUTE:
                until = datetime.now(timezone.utc) + timedelta(minutes=15)
                await self.bot.restrict_chat_member(
                    ctx.chat_id,
                    ctx.user_id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=until,
                )
                await self.bot.send_message(
                    ctx.chat_id,
                    f"🔇 User @{ctx.username or ctx.user_id} muted: {verdict.reason}",
                )
            elif verdict.action == ActionType.BAN:
                await self.bot.ban_chat_member(ctx.chat_id, ctx.user_id)
                await self.bot.send_message(
                    ctx.chat_id,
                    f"🚫 User @{ctx.username or ctx.user_id} banned: {verdict.reason}",
                )
        except Exception as exc:  # pragma: no cover - network errors
            logger.error(
                "telegram_decision_error",
                error=str(exc),
                action=verdict.action.value,
                chat_id=ctx.chat_id,
            )

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
        admin_chats = await self._available_admin_chats(message.from_user.id)
        if not admin_chats:
            await message.reply(
                "No chats detected yet. Add me to groups where you are admin and send any message there, "
                "or type `set <chat_id>` to choose a chat manually.",
                parse_mode="Markdown",
            )
            return
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=title or str(chat_id),
                        callback_data=f"panel:chat:{chat_id}",
                    )
                ]
                for chat_id, title in admin_chats[:12]
            ]
        )
        await message.reply(
            "Select a chat to manage rules:",
            reply_markup=keyboard,
        )

    async def _handle_panel_select(self, callback: CallbackQuery) -> None:
        await callback.answer()
        chat_id = int(callback.data.split(":")[2])
        admin_chats = dict(await self._available_admin_chats(callback.from_user.id))
        if chat_id not in admin_chats:
            await callback.message.edit_text("You are not an admin in that chat or it is unavailable.")
            return
        self._admin_sessions[callback.from_user.id] = {"chat_id": chat_id}
        rules = await self.coordinator.list_rules(chat_id)
        rule_lines = "\n".join(
            f"- {rule.rule_id} [{rule.layer.value}] {rule.action.value}: {rule.description}"
            for rule in rules
        ) or "No rules configured yet."
        help_text = f"Now controlling chat: {admin_chats[chat_id]} ({chat_id}).\n\n{PANEL_HELP}"
        await callback.message.edit_text(
            f"{help_text}\n\nCurrent rules:\n{rule_lines}",
            parse_mode="Markdown",
        )

    async def _handle_admin_text(self, message: Message) -> None:
        if message.text and message.text.startswith("/"):
            return  # slash commands handled separately
        session = self._admin_sessions.get(message.from_user.id)
        if not session:
            await message.answer("Send /panel to choose a chat to manage.")
            return
        chat_id = session["chat_id"]
        text = (message.text or "").strip()
        if text.lower() in {"help", ""}:
            await message.answer(PANEL_HELP, parse_mode="Markdown")
            return
        if text.lower() == "list":
            rules = await self.coordinator.list_rules(chat_id)
            if not rules:
                await message.answer("No rules configured yet.")
                return
            lines = [
                f"{rule.rule_id} [{rule.layer.value}] {rule.action.value}: {rule.description}"
                for rule in rules
            ]
            await message.answer("\n".join(lines))
            return
        if text.lower().startswith("remove"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                await message.answer("Usage: remove <rule_id>")
                return
            if chat_id is not None and not await self._ensure_admin(chat_id, message.from_user.id):
                await message.answer("You are not an admin in that chat.")
                return
            try:
                await self.coordinator.remove_rule(parts[1])
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("remove_rule_failed", error=str(exc))
                await message.answer("Failed to remove rule. Check logs.")
                return
            await message.answer(f"Removed rule {parts[1]}")
            return
        if text.lower().startswith("set"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                await message.answer("Usage: set <chat_id>")
                return
            try:
                new_chat_id = int(parts[1])
            except ValueError:
                await message.answer("Chat ID must be an integer.")
                return
            if not await self._ensure_admin(new_chat_id, message.from_user.id):
                await message.answer("You are not an admin in that chat.")
                return
            self._admin_sessions[message.from_user.id] = {"chat_id": new_chat_id}
            await message.answer(f"Switched to chat {new_chat_id}. Type `list` to see rules.")
            return
        if text.lower().startswith("add-global"):
            await self._admin_add_rule(message, chat_id=None, command=text)
            return
        if text.lower().startswith("add"):
            await self._admin_add_rule(message, chat_id=chat_id, command=text)
            return
        await message.answer("Unknown command. Type 'help' for instructions.")

    async def _admin_add_rule(self, message: Message, chat_id: Optional[int], command: str) -> None:
        tokens = shlex.split(command)
        if len(tokens) < 3:
            await message.answer("Usage: add <action[:duration]> [layer=...] [type=...] [category=...] <description>")
            return
        _, action_token, *rest_tokens = tokens
        try:
            action, duration = self._parse_action_token(action_token)
        except ValueError as exc:
            await message.answer(str(exc))
            return

        try:
            layer_override, rule_type_override, category, pattern, description = self._extract_rule_metadata(rest_tokens)
        except ValueError as exc:
            await message.answer(str(exc))
            return

        if duration is None and description:
            first_word = description.split(maxsplit=1)[0]
            if self._looks_like_duration(first_word):
                try:
                    duration = self._parse_duration(first_word)
                except ValueError as exc:
                    await message.answer(str(exc))
                    return
                description = description.split(maxsplit=1)[1] if ' ' in description else ''
        if not description:
            await message.answer("Please provide rule description.")
            return
        if chat_id is not None and not await self._ensure_admin(chat_id, message.from_user.id):
            await message.answer("You are not an admin in that chat.")
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
            logger.error("panel_add_rule_failed", error=str(exc))
            await message.answer("Failed to add rule. Check logs for details.")
            return
        scope_label = "global" if chat_id is None else f"chat {chat_id}"
        await message.answer(f"Rule {rule.rule_id} added for {scope_label}.")
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
        base = token
        duration = None
        if ":" in token:
            base, duration_part = token.split(":", 1)
            if not duration_part:
                raise ValueError("Duration must follow the action, e.g. mute:10m")
            duration = self._parse_duration(duration_part)
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
