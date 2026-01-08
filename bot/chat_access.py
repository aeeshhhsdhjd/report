from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

# Import Pyrogram errors safely
try:
    from pyrogram.errors import (
        BadRequest, ChannelInvalid, ChannelPrivate, ChatAdminRequired,
        ChatIdInvalid, FloodWait, InviteHashExpired, InviteHashInvalid,
        PeerFlood, PeerIdInvalid, RPCError, UsernameInvalid,
        UsernameNotOccupied, UserAlreadyParticipant,
    )
except ImportError:
    # Fallback for environments where Pyrogram isn't installed during build
    class RPCError(Exception): pass
    class FloodWait(RPCError): value = 0
    BadRequest = ChannelInvalid = ChannelPrivate = ChatIdInvalid = \
    PeerIdInvalid = UsernameInvalid = UsernameNotOccupied = \
    InviteHashExpired = InviteHashInvalid = ChatAdminRequired = \
    PeerFlood = UserAlreadyParticipant = RPCError

_FAILURE_TTL = timedelta(minutes=45)
_LOG_THROTTLE = timedelta(minutes=10)
_INVITE_RE = re.compile(r"(?:t\.me/|telegram\.me/)(?:\+|joinchat/)([a-zA-Z0-9_-]+)")

@dataclass
class FailureRecord:
    reason: str
    expires_at: datetime

_failure_cache: dict[str, FailureRecord] = {}
_log_cooldowns: dict[str, datetime] = {}
_invite_locks: dict[str, asyncio.Lock] = {}

def _extract_invite_hash(link: str) -> str | None:
    """Extracts the hash from a variety of Telegram invite formats."""
    match = _INVITE_RE.search(link)
    return match.group(1) if match else None

def _now() -> datetime:
    return datetime.now(timezone.utc)

def _clean_failure_cache() -> None:
    now = _now()
    expired = [k for k, v in _failure_cache.items() if v.expires_at <= now]
    for k in expired:
        del _failure_cache[k]

async def join_by_invite_safe(client: Any, invite_link: str, *, max_retries: int = 2) -> dict[str, Any]:
    """Join via invite with deduped locks to prevent 'Join Spam' bans."""
    invite_hash = _extract_invite_hash(invite_link)
    if not invite_hash:
        return {"ok": False, "status": "INVALID_LINK", "detail": "Regex failed to find hash"}

    # Use a lock per invite hash so 100 sessions don't join at once
    lock = _invite_locks.setdefault(invite_hash, asyncio.Lock())
    async with lock:
        for attempt in range(1, max_retries + 1):
            try:
                # Use the hash directly for the join call
                await client.join_chat(invite_hash)
                return {"ok": True, "status": "JOINED"}
            except UserAlreadyParticipant:
                return {"ok": True, "status": "ALREADY_MEMBER"}
            except FloodWait as e:
                if attempt == max_retries:
                    return {"ok": False, "status": "FLOOD", "wait": e.value}
                # Add jitter to prevent synchronized retry spikes
                await asyncio.sleep(e.value + random.uniform(1.1, 3.5))
            except (InviteHashInvalid, InviteHashExpired):
                return {"ok": False, "status": "DEAD_LINK"}
            except RPCError as e:
                return {"ok": False, "status": "RPC_ERROR", "detail": str(e)}
    return {"ok": False, "status": "EXHAUSTED"}

async def resolve_chat_safe(client: Any, chat_id: Any, invite_link: str | None = None) -> tuple[Any | None, str | None]:
    """Resolves a chat, attempting a join if private access is denied."""
    _clean_failure_cache()
    
    # Normalize key for cache
    cache_key = str(chat_id).lower()
    if cache_key in _failure_cache:
        if _failure_cache[cache_key].expires_at > _now():
            return None, f"cached_failure: {_failure_cache[cache_key].reason}"

    try:
        chat = await client.get_chat(chat_id)
        return chat, None
    except (PeerIdInvalid, ChannelPrivate, ChatIdInvalid):
        if invite_link:
            res = await join_by_invite_safe(client, invite_link)
            if res["ok"]:
                try:
                    chat = await client.get_chat(chat_id)
                    return chat, None
                except Exception as e:
                    return None, f"failed_after_join: {str(e)}"
            return None, f"join_failed: {res['status']}"
        return None, "access_denied_no_invite"
    except FloodWait as e:
        return None, f"flood: {e.value}s"
    except Exception as e:
        _failure_cache[cache_key] = FailureRecord(str(e), _now() + _FAILURE_TTL)
        return None, str(e)
