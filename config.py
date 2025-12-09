"""Simple configuration for the Telegram bot."""
from __future__ import annotations

import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
LOG_CHAT_ID = int(os.getenv("LOG_CHAT_ID", "0") or 0)
ADMIN_USER_IDS = tuple(int(user_id) for user_id in os.getenv("ADMIN_USER_IDS", "").split(",") if user_id)
