"""Simple configuration for the Telegram bot."""
from __future__ import annotations

import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Pyrogram API credentials for reporting sessions
API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "")

# Optional MongoDB URI for session/report persistence
MONGO_URI = os.getenv("MONGO_URI", "")
