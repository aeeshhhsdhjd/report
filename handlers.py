from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections import deque
from datetime import datetime
from time import monotonic
from typing import Callable, Tuple, Any

from pyrogram import Client, filters
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import FloodWait, RPCError, UserAlreadyParticipant
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
from logging_utils import log_error, log_report_summary, log_user_start, send_log
from report import send_report
from session_bot import prune_sessions
from state import QueueEntry, ReportQueue, StateManager, UserState
from sudo import is_owner
from ui import (
    REPORT_REASONS,
    owner_panel,
    queued_message,
    reason_keyboard,
    report_type_keyboard,
    sudo_panel,
)
from bot.utils import resolve_chat_id


def _normalize_chat_id(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_link(link: str, is_private: bool) -> Tuple[Any, int]:
    """
    Parses Telegram links into (chat_id, message_id).
    Returns chat_id as int for private/IDs and string for usernames.
    """
    cleaned = link.replace("https://t.me/", "").replace("http://t.me/", "").replace("t.me/", "").strip("/")
    parts = [part for part in cleaned.split("/") if part]
    if len(parts) < 2:
        raise ValueError("Invalid link format")

    # Handle private 'c' links or forced private mode
    if is_private or parts[0] == "c":
        idx = 1 if parts[0] == "c" else 0
        try:
            # Convert to -100 format for private channels
            chat_id = int(f"-100{parts[idx]}")
            message_id = int(parts[idx + 1])
            return chat_id, message_id
        except (ValueError, IndexError):
            raise ValueError("Malformed private link")

    # Handle public username links
    chat_id = parts[0]
    try:
        message_id = int(parts[1])
        return chat_id, message_id
    except (ValueError, IndexError):
        raise ValueError("Malformed public link")


def register_handlers(app: Client, persistence, states: StateManager, queue: ReportQueue) -> None:
    """Register all command and callback handlers."""

    async def _log_stage(stage: str, detail: str) -> None:
        logs_id = await persistence.get_logs_group_id()
        if logs_id:
            await send_log(app, logs_id, f"🛰 {stage}\n{detail}")

    async def _ensure_admin(chat_id: int) -> bool:
        try:
            me = await app.get_me()
            member = await app.get_chat_member(chat_id, me.id)
            return member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}
        except Exception:
            return False

    async def _wrap_errors(func: Callable, *args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as exc:
            logging.exception("Handler error")
            logs_id = await persistence.get_logs_group_id()
            await log_error(app, logs_id, exc, config.OWNER_ID)

    async def _is_sudo_user(user_id: int | None) -> bool:
        if user_id is None:
            return False
        if is_owner(user_id):
            return True
        sudo_users = await persistence.get_sudo_users()
        return user_id in (sudo_users or set(config.SUDO_USERS))

    # --- COMMANDS ---

    @app.on_message(filters.command("start") & filters.private)
    async def start_handler(_: Client, message: Message):
        await _wrap_errors(_handle_start, message)

    async def _handle_start(message: Message):
        user_id = message.from_user.id
        await persistence.add_known_chat(message.chat.id)
        await log_user_start(app, await persistence.get_logs_group_id(), message)
        
        if is_owner(user_id):
            await message.reply_text("Welcome, Owner! Use the panel below to manage the bot.", reply_markup=owner_panel())
        elif await _is_sudo_user(user_id):
            await message.reply_text("👋 Bot is active. Ready to start reporting?", reply_markup=sudo_panel(user_id))
        else:
            await message.reply_text(f"🚫 Unauthorized. Contact owner.\nYour ID: `{user_id}`")

    @app.on_message(filters.command(["set_session", "set_log"]) & filters.group)
    async def config_groups_handler(_: Client, message: Message):
        if not is_owner(message.from_user.id): return
        if not await _ensure_admin(message.chat.id):
            return await message.reply_text("❌ Promote the bot to Admin first.")
        
        if "set_session" in message.text:
            await persistence.save_session_group_id(message.chat.id)
            await message.reply_text("✅ This group is now the Session Ingestor.")
        else:
            await persistence.save_logs_group_id(message.chat.id)
            await message.reply_text("📝 This group is now the Log Channel.")

    # --- CALLBACKS ---

    @app.on_callback_query(filters.regex(r"^sudo:start$"))
    async def start_flow_cb(_: Client, query: CallbackQuery):
        state = states.get(query.from_user.id)
        state.reset()
        sessions = await prune_sessions(persistence)
        if not sessions:
            return await query.answer("No sessions found in database!", show_alert=True)
        
        state.stage = "type"
        await query.message.edit_text("Select target visibility:", reply_markup=report_type_keyboard())

    @app.on_callback_query(filters.regex(r"^report:type:(public|private)$"))
    async def visibility_cb(_: Client, query: CallbackQuery):
        state = states.get(query.from_user.id)
        state.report_type = query.data.split(":")[-1]
        state.stage = "awaiting_count"
        await query.message.edit_text(f"How many report attempts? ({config.MIN_REPORTS}-{config.MAX_REPORTS})")

    @app.on_callback_query(filters.regex(r"^report:reason:[a-z_]+$"))
    async def reason_cb(_: Client, query: CallbackQuery):
        state = states.get(query.from_user.id)
        key = query.data.split(":")[-1]
        label, code = REPORT_REASONS.get(key, ("Other", 9))
        state.reason_code = code
        state.reason_text = label
        
        if key == "other":
            state.stage = "awaiting_reason_text"
            await query.message.reply_text("Type the custom reason description:")
        else:
            await _begin_report(query.message, state)

    # --- INPUT HANDLER ---

    @app.on_message(filters.private & filters.text & ~filters.command(["start"]))
    async def state_router(_: Client, message: Message):
        if not await _is_sudo_user(message.from_user.id): return
        state = states.get(message.from_user.id)

        if state.stage == "awaiting_count":
            if message.text.isdigit():
                val = int(message.text)
                if config.MIN_REPORTS <= val <= config.MAX_REPORTS:
                    state.report_count = val
                    state.stage = "awaiting_private_join" if state.report_type == "private" else "awaiting_link"
                    prompt = "🔗 Paste the Invite Link:" if state.stage == "awaiting_private_join" else "🔗 Paste the Message Link:"
                    await message.reply_text(prompt)
                else:
                    await message.reply_text(f"Please stay between {config.MIN_REPORTS} and {config.MAX_REPORTS}.")

        elif state.stage == "awaiting_private_join":
            await message.reply_text("🔄 Attempting to join sessions to target...")
            if await _join_sessions_to_chat(message.text, message):
                state.stage = "awaiting_link"
                await message.reply_text("✅ Joined. Now send the specific Message Link to report:")
            else:
                await message.reply_text("❌ Failed to join. Check the link or session status.")

        elif state.stage == "awaiting_link":
            if "t.me/" in message.text:
                state.target_link = message.text.strip()
                state.stage = "awaiting_reason"
                await message.reply_text("Select report reason:", reply_markup=reason_keyboard())

        elif state.stage == "awaiting_reason_text":
            state.reason_text = message.text
            await _begin_report(message, state)

    # --- ENGINE ---

    async def _begin_report(message: Message, state: UserState):
        state.stage = "queued"
        state.started_at = monotonic()
        
        async def notify(pos: int):
            if pos > 1:
                await message.reply_text(queued_message(pos))

        entry = QueueEntry(state.user_id, lambda: _run_report_job(message, state), notify)
        await queue.enqueue(entry)
        await message.reply_text("⏳ Your request is in the queue.")

    async def _run_report_job(message: Message, state: UserState):
        try:
            sessions = await persistence.get_sessions()
            chat_ref, msg_id = _parse_link(state.target_link, state.report_type == "private")
            
            success, failed = 0, 0
            progress = await message.reply_text("🚀 Starting Report Flood...")

            for i in range(state.report_count):
                s_str = sessions[i % len(sessions)]
                client = Client(
                    name=f"worker_{uuid.uuid4().hex[:6]}",
                    api_id=config.API_ID,
                    api_hash=config.API_HASH,
                    session_string=s_str,
                    in_memory=True
                )
                try:
                    await client.start()
                    # Invokes send_report from report.py
                    res = await send_report(client, chat_ref, msg_id, state.reason_code, state.reason_text)
                    if res: success += 1
                    else: failed += 1
                except Exception:
                    failed += 1
                finally:
                    with contextlib.suppress(Exception):
                        await client.stop()
                
                if (i + 1) % 5 == 0 or (i + 1) == state.report_count:
                    with contextlib.suppress(Exception):
                        await progress.edit_text(
                            f"📡 **Live Progress**\n"
                            f"Attempts: {i+1}/{state.report_count}\n"
                            f"✅ Success: {success} | ❌ Failed: {failed}"
                        )
                await asyncio.sleep(0.3) # Avoid local flood errors

            elapsed = monotonic() - state.started_at
            await message.reply_text(f"✅ **Flood Complete**\nSuccess: {success}\nTime: {elapsed:.1f}s")
            
            logs_id = await persistence.get_logs_group_id()
            await log_report_summary(app, logs_id, message.from_user, state.target_link, elapsed, success > 0)
            
        except Exception as e:
            logging.exception("Reporting task crashed")
            await message.reply_text(f"❌ Engine Error: {e}")
        finally:
            states.reset(state.user_id)

    async def _join_sessions_to_chat(invite: str, message: Message) -> bool:
        sessions = await persistence.get_sessions()
        joined = 0
        for s in sessions:
            c = Client(uuid.uuid4().hex[:6], config.API_ID, config.API_HASH, session_string=s, in_memory=True)
            try:
                await c.start()
                await c.join_chat(invite)
                joined += 1
            except (UserAlreadyParticipant, RPCError):
                joined += 1
            except: pass
            finally: await c.stop()
        return joined > 0
