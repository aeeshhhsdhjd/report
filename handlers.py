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

def register_handlers(app: Client, persistence, states: StateManager, queue: ReportQueue) -> None:
    """Register all command and callback handlers."""

    # --- Internal Helpers ---

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

    async def _resolve_target_across_sessions(target_link: str, sessions: list[str]):
        last_error = None
        available_sessions = []
        resolved_chat_id = None

        for idx, session in enumerate(sessions):
            client = Client(
                name=f"resolver_{uuid.uuid4().hex[:8]}",
                api_id=config.API_ID,
                api_hash=config.API_HASH,
                session_string=session,
                in_memory=True
            )
            try:
                await client.start()
                chat_id = await resolve_chat_id(client, target_link)
                if chat_id:
                    resolved_chat_id = chat_id
                    available_sessions.append(session)
                    break # Found one, good enough to resolve
            except Exception as e:
                last_error = str(e)
            finally:
                with contextlib.suppress(Exception):
                    await client.stop()
        
        return resolved_chat_id, available_sessions if available_sessions else sessions, last_error

    # --- Command Handlers ---

    @app.on_message(filters.command("start") & filters.private)
    async def start_handler(_: Client, message: Message):
        await _wrap_errors(_handle_start, message)

    async def _handle_start(message: Message):
        user_id = message.from_user.id
        await persistence.add_known_chat(message.chat.id)
        
        if is_owner(user_id):
            await message.reply_text("Welcome, Owner!", reply_markup=owner_panel())
        elif await _is_sudo_user(user_id):
            await message.reply_text("👋 Ready to report?", reply_markup=sudo_panel(user_id))
        else:
            await message.reply_text("🚫 Unauthorized access.")

    @app.on_message(filters.command("addsudo") & filters.private)
    async def add_sudo_handler(_: Client, message: Message):
        if not is_owner(message.from_user.id): return
        parts = message.text.split()
        if len(parts) > 1 and parts[1].isdigit():
            await persistence.add_sudo_user(int(parts[1]))
            await message.reply_text(f"✅ Added {parts[1]} to sudo.")

    @app.on_message(filters.command("set_session") & filters.group)
    async def set_session_group(_: Client, message: Message):
        if not is_owner(message.from_user.id): return
        await persistence.save_session_group_id(message.chat.id)
        await message.reply_text("✅ This group is now the session manager.")

    # --- Callback Handlers ---

    @app.on_callback_query(filters.regex(r"^sudo:start$"))
    async def start_report_cb(_: Client, query: CallbackQuery):
        state = states.get(query.from_user.id)
        state.reset()
        sessions = await prune_sessions(persistence)
        if not sessions:
            await query.answer("No sessions available!", show_alert=True)
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
            await query.message.reply_text("Type your custom reason:")
        else:
            await _begin_report(query.message, state)

    # --- Text Logic ---

    @app.on_message(filters.private & filters.text & ~filters.command(["start", "addsudo"]))
    async def text_router(_: Client, message: Message):
        state = states.get(message.from_user.id)
        
        if state.stage == "awaiting_count":
            if message.text.isdigit():
                count = int(message.text)
                if config.MIN_REPORTS <= count <= config.MAX_REPORTS:
                    state.report_count = count
                    state.stage = "awaiting_link" if state.report_type == "public" else "awaiting_private_join"
                    prompt = "Send the message link:" if state.stage == "awaiting_link" else "Send the private invite link:"
                    await message.reply_text(prompt)
                else:
                    await message.reply_text("Invalid range.")
        
        elif state.stage == "awaiting_private_join":
            # Logic for joining sessions
            success = await _join_sessions_to_chat(message.text, message)
            if success:
                state.stage = "awaiting_link"
                await message.reply_text("✅ Joined. Now send the message link:")

        elif state.stage == "awaiting_link":
            if "t.me/" in message.text:
                state.target_link = message.text.strip()
                state.stage = "awaiting_reason"
                await message.reply_text("Select Reason:", reply_markup=reason_keyboard())

    # --- Reporting Engine ---

    async def _begin_report(message: Message, state: UserState):
        state.stage = "queued"
        state.started_at = monotonic()
        
        entry = QueueEntry(
            user_id=state.user_id,
            job=lambda: _run_report_job(message, state),
            notify_position=lambda p: None
        )
        await queue.enqueue(entry)
        await message.reply_text("⏳ Added to queue...")

    async def _run_report_job(message: Message, state: UserState):
        try:
            stats = await _execute_report(message, state)
            await message.reply_text(f"✅ Report Finished\nSuccess: {stats['success_count']}\nFailed: {stats['failure_count']}")
        finally:
            states.reset(state.user_id)

    async def _execute_report(message: Message, state: UserState):
        sessions = await persistence.get_sessions()
        chat_ref, msg_id = _parse_link(state.target_link, state.report_type == "private")
        
        # Resolve the chat ID once to avoid repeated peer flood
        resolved_id, usable_sessions, _ = await _resolve_target_across_sessions(state.target_link, sessions)
        
        success_count = 0
        failure_count = 0
        
        progress_msg = await message.reply_text("🚀 Starting report flood...")

        for i in range(state.report_count):
            session = sessions[i % len(sessions)]
            client = Client(f"run_{uuid.uuid4().hex[:8]}", session_string=session, in_memory=True, api_id=config.API_ID, api_hash=config.API_HASH)
            try:
                await client.start()
                # Use resolve_id if found, else fallback to chat_ref
                ok = await send_report(client, resolved_id or chat_ref, msg_id, state.reason_code, state.reason_text)
                if ok: success_count += 1
                else: failure_count += 1
            except Exception:
                failure_count += 1
            finally:
                await client.stop()
            
            if i % 5 == 0:
                with contextlib.suppress(Exception):
                    await progress_msg.edit_text(f"Progress: {i+1}/{state.report_count}\n✅ {success_count} | ❌ {failure_count}")
        
        return {"success_count": success_count, "failure_count": failure_count}

    async def _join_sessions_to_chat(invite_link: str, message: Message) -> bool:
        sessions = await persistence.get_sessions()
        joined = 0
        for session in sessions:
            client = Client(f"joiner_{uuid.uuid4().hex[:8]}", session_string=session, in_memory=True, api_id=config.API_ID, api_hash=config.API_HASH)
            try:
                await client.start()
                await client.join_chat(invite_link)
                joined += 1
            except (UserAlreadyParticipant, RPCError):
                joined += 1
            finally:
                await client.stop()
        return joined > 0

# --- Link Parser Helper ---
def _parse_link(link: str, is_private: bool) -> Tuple[Any, int]:
    link = link.replace("https://t.me/", "").replace("t.me/", "")
    parts = link.split("/")
    
    try:
        if "c/" in link or (is_private and parts[0] == "c"):
            # Private: t.me/c/1234567/10
            return int(f"-100{parts[1]}"), int(parts[2])
        else:
            # Public: t.me/username/10
            return parts[0], int(parts[1])
    except (ValueError, IndexError):
        raise ValueError("Malformed Telegram link")
