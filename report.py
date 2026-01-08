"""
Reporting helpers built on top of :class:`pyrogram.Client`.

This module exposes a small `send_report` helper that calls the raw
MTProto ``messages.Report`` RPC with clean ergonomics.

Concurrency, retries, flood-wait handling, and orchestration are expected
to be handled by higher-level flows (e.g. main.py).
"""
from __future__ import annotations

import asyncio

from pyrogram import Client
from pyrogram.errors import (
    BadRequest,
    FloodWait,
    MessageIdInvalid,
    PeerIdInvalid,
    RPCError,
    UsernameInvalid,
)
from pyrogram.raw.functions.contacts import ResolveUsername
from pyrogram.raw.functions.messages import Report
from pyrogram.raw.types import (
    InputPeerChannel,
    InputPeerChat,
    InputPeerUser,
    InputReportReasonSpam,
    InputReportReasonViolence,
    InputReportReasonPornography,
    InputReportReasonChildAbuse,
    InputReportReasonCopyright,
    InputReportReasonGeoIrrelevant,
    InputReportReasonFake,
    InputReportReasonIllegalDrugs,
    InputReportReasonPersonalDetails,
    InputReportReasonOther,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _build_reason(reason: int | object) -> object:
    """Return a Pyrogram InputReportReason instance from a code or object."""

    reason_map = {
        0: InputReportReasonSpam,
        1: InputReportReasonViolence,
        2: InputReportReasonPornography,
        3: InputReportReasonChildAbuse,
        4: InputReportReasonCopyright,
        5: InputReportReasonGeoIrrelevant,
        6: InputReportReasonFake,
        7: InputReportReasonIllegalDrugs,
        8: InputReportReasonPersonalDetails,
        9: InputReportReasonOther,
    }

    # Already a raw reason object
    if hasattr(reason, "write"):
        return reason

    try:
        reason_int = int(reason)
    except Exception:
        return InputReportReasonOther()

    reason_cls = reason_map.get(reason_int, InputReportReasonOther)
    return reason_cls()


async def _resolve_peer_for_report(client: Client, chat_id):
    """Resolve chat/user identifiers into raw InputPeer objects.

    Supports:
    - int IDs
    - numeric strings (e.g. "-100...")
    - usernames (@username or "username")
    """

    # Raw peer already provided
    if hasattr(chat_id, "write"):
        return chat_id

    # Allow numeric strings
    if isinstance(chat_id, str) and chat_id.lstrip("-+").isdigit():
        chat_id = int(chat_id)

    # First try Pyrogram's resolver
    try:
        return await client.resolve_peer(chat_id)
    except UsernameInvalid:
        pass
    except ValueError as exc:
        raise BadRequest(f"Invalid target for reporting: {exc}") from exc

    # Fallback: raw username resolution
    username = str(chat_id).lstrip("@")
    try:
        resolved = await client.invoke(ResolveUsername(username=username))

        if resolved.users:
            user = resolved.users[0]
            return InputPeerUser(
                user_id=user.id,
                access_hash=user.access_hash,
            )

        if resolved.chats:
            chat = resolved.chats[0]
            if hasattr(chat, "access_hash"):
                return InputPeerChannel(
                    channel_id=chat.id,
                    access_hash=chat.access_hash,
                )
            return InputPeerChat(chat_id=chat.id)

    except Exception:
        pass

    raise BadRequest("Unable to resolve the target for reporting.")


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

async def send_report(
    client: Client,
    chat_id,
    message_id: int,
    reason: int | object,
    reason_text: str,
) -> bool:
    """Send a report against a specific Telegram message.

    Returns:
        True  -> report sent or message already invalid/deleted
        False -> unexpected error occurred
    """

    try:
        reason_obj = _build_reason(reason)

        # Resolve peer early so resolution errors are consistent
        resolved_peer = await _resolve_peer_for_report(client, chat_id)

        await client.invoke(
            Report(
                peer=resolved_peer,
                id=[message_id],
                reason=reason_obj,
                message=reason_text,
            )
        )

        return True

    except MessageIdInvalid:
        print(
            f"[{getattr(client, 'name', 'unknown')}] "
            f"Message ID {message_id} is invalid or deleted. Skipping."
        )
        return True

    except (FloodWait, BadRequest, PeerIdInvalid, RPCError):
        # Let higher-level logic decide how to handle these
        raise

    except Exception as exc:  # defensive fallback
        print(
            f"[{getattr(client, 'name', 'unknown')}] "
            f"Unexpected error while reporting: {exc}"
        )
        return False
