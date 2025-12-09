"""Simple configuration for the Telegram bot."""
from __future__ import annotations

import os

BOT_TOKEN = os.getenv("8560252019:AAGEn7LTiKXdiVHMc7eHuXDm0rAuLdvZNUo", "")

# Pyrogram API credentials for reporting sessions
API_ID = int(os.getenv("27989579", "0") or 0)
API_HASH = os.getenv("64742ebe270a7d202150134d66397839", "")

# Optional MongoDB URI for session/report persistence
MONGO_URI = os.getenv("mongodb+srv://annieregain:firstowner8v@anniere.ht2en.mongodb.net/?retryWrites=true&w=majority&appName=AnnieRE
", "")
