from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections import deque
from datetime import datetime
from io import BytesIO
from time import monotonic
from typing import Callable, Tuple, Any

from pyrogram import Client, filters
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import FloodWait, RPCError, UserAlreadyParticipant
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
from logging_utils import log_error, log_report_summary, log_user_start, send_log
from report import send_report
from session_bot import (
    SessionIdentity,
    extract_sessions_from_text,
    fetch_session_identity,
    prune_sessions,
    validate_session_string,
)
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
    if value is None: return None
    if isinstance(value, int): return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None

def _parse_link(link: str, is_private: bool) -> Tuple[Any, int]:
    cleaned = link.replace("https://t.me/", "").replace("http://t.me/", "").replace("t.me/", "").strip("/")
    parts = [part for part in cleaned.split("/") if part]
    if len(parts) < 2:
        raise ValueError("Invalid link")

    if is_private or parts[0] == "c":
        # Handle t.me/c/12345/67 or t.me/12345/67
        idx = 1 if parts[0] == "c" else 0
        chat_id = int(f"-100{parts[idx]}")
        message_id = int(parts[idx+1])
        return chat_id, message_id
    
    # Public: t.me/username/123
    chat_id = parts[0].lstrip("@")
    message_id = int(parts[1])
    return chat_id, message_id

def register_handlers(app: Client, persistence, states: StateManager, queue: ReportQueue) -> None:
    """Register all command and callback handlers."""

    session_tokens: dict[str, str] = {}

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
        if user_id is None: return False
        if is_owner(user_id): return True
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
            await message.reply_text("Welcome, Owner!", reply_markup=owner_panel())
        elif await _is_sudo_user(user_id):
            await message.reply_text("👋 Ready to report?", reply_markup=sudo_panel(user_id))
        else:
            await message.reply_text(f"🚫 Unauthorized. ID: {user_id}")

    @app.on_message(filters.command("addsudo") & filters.private)
    async def addsudo_handler(_: Client, message: Message):
        if not is_owner(message.from_user.id): return
        parts = message.text.split()
        if len(parts) > 1 and parts[1].isdigit():
            await persistence.add_sudo_user(int(parts[1]))
            await message.reply_text(f"✅ Added {parts[1]} to Sudo.")

    @app.on_message(filters.command("set_session") & filters.group)
    async def set_session_handler(_: Client, message: Message):
        if not is_owner(message.from_user.id): return
        if await _ensure_admin(message.chat.id):
            await persistence.save_session_group_id(message.chat.id)
            await message.reply_text("✅ Group set as Session Manager.")

    # --- CALLBACKS ---

    @app.on_callback_query(filters.regex(r"^sudo:start$"))
    async def start_report_cb(_: Client, query: CallbackQuery):
        state = states.get(query.from_user.id)
        state.reset()
        sessions = await prune_sessions(persistence)
        if not sessions:
            await query.answer("No sessions found!", show_alert=True)
            return
        state.stage = "type"
        await query.message.edit_text("Select Report Type:", reply_markup=report_type_keyboard())

    @app.on_callback_query(filters.regex(r"^report:type:(public|private)$"))
    async def type_cb(_: Client, query: CallbackQuery):
        state = states.get(query.from_user.id)
        state.report_type = query.data.split(":")[-1]
        state.stage = "awaiting_count"
        await query.message.edit_text(f"How many reports? ({config.MIN_REPORTS}-{config.MAX_REPORTS})")

    @app.on_callback_query(filters.regex(r"^report:reason:[a-z_]+$"))
    async def reason_cb(_: Client, query: CallbackQuery):
        state = states.get(query.from_user.id)
        key = query.data.split(":")[-1]
        label, code = REPORT_REASONS.get(key, ("Other", 9))
        state.reason_code = code
        state.reason_text = label
        
        if key == "other":
            state.stage = "awaiting_reason_text"
            await query.message.reply_text("Enter custom reason text:")
        else:
            await _begin_report(query.message, state)

    # --- TEXT ROUTING ---

    @app.on_message(filters.private & filters.text & ~filters.command(["start", "addsudo", "set_session"]))
    async def text_router(_: Client, message: Message):
        if not await _is_sudo_user(message.from_user.id): return
        state = states.get(message.from_user.id)
        
        if state.stage == "awaiting_count":
            if message.text.isdigit():
                count = int(message.text)
                if config.MIN_REPORTS <= count <= config.MAX_REPORTS:
                    state.report_count = count
                    state.stage = "awaiting_private_join" if state.report_type == "private" else "awaiting_link"
                    await message.reply_text("Send the link/invite:")
            else:
                await message.reply_text("Please send a number.")

        elif state.stage == "awaiting_private_join":
            await message.reply_text("⏳ Joining sessions...")
            if await _join_sessions_to_chat(message.text, message):
                state.stage = "awaiting_link"
                await message.reply_text("✅ Joined. Send the message link:")

        elif state.stage == "awaiting_link":
            if "t.me/" in message.text:
                state.target_link = message.text.strip()
                state.stage = "awaiting_reason"
                await message.reply_text("Select Reason:", reply_markup=reason_keyboard())

        elif state.stage == "awaiting_reason_text":
            state.reason_text = message.text
            await _begin_report(message, state)

    # --- ENGINE ---

    async def _begin_report(message: Message, state: UserState):
        state.stage = "queued"
        state.started_at = monotonic()
        entry = QueueEntry(state.user_id, lambda: _run_report_job(message, state), lambda p: None)
        await queue.enqueue(entry)
        await message.reply_text("⏳ Queued...")

    async def _run_report_job(message: Message, state: UserState):
        try:
            sessions = await persistence.get_sessions()
            chat_ref, msg_id = _parse_link(state.target_link, state.report_type == "private")
            
            success = 0
            progress = await message.reply_text("🚀 Starting...")

            for i in range(state.report_count):
                s_str = sessions[i % len(sessions)]
                client = Client(uuid.uuid4().hex, session_string=s_str, in_memory=True, api_id=config.API_ID, api_hash=config.API_HASH)
                try:
                    await client.start()
                    if await send_report(client, chat_ref, msg_id, state.reason_code, state.reason_text):
                        success += 1
                except: pass
                finally: await client.stop()
                
                if i % 5 == 0:
                    with contextlib.suppress(Exception):
                        await progress.edit_text(f"Progress: {i+1}/{state.report_count}\nSuccess: {success}")

            await progress.edit_text(f"✅ Done. {success} reports sent.")
        finally:
            states.reset(state.user_id)

    async def _join_sessions_to_chat(invite: str, message: Message) -> bool:
        sessions = await persistence.get_sessions()
        joined = 0
        for s in sessions:
            c = Client(uuid.uuid4().hex, session_string=s, in_memory=True, api_id=config.API_ID, api_hash=config.API_HASH)
            try:
                await c.start()
                await c.join_chat(invite)
                joined += 1
            except: pass
            finally: await c.stop()
        return joined > 0
