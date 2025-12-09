from __future__ import annotations

"""Configuration helpers for the Reaction Reporter Bot.

Values are loaded from environment variables to keep secrets out of the
codebase. Provide BOT_TOKEN, API_ID, API_HASH, and optionally MONGO_URI via
exported environment variables or a `.env` loader in your host runtime.
"""

import os
from typing import Final


def _int_from_env(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


BOT_TOKEN: Final[str] = os.getenv("BOT_TOKEN", "")
API_ID: Final[int | None] = _int_from_env("API_ID")
API_HASH: Final[str] = os.getenv("API_HASH", "")
MONGO_URI: Final[str] = os.getenv("MONGO_URI", "")

# Stored hash of the expected author identifier. The verification step is kept
# as a lightweight tamper check but can be safely removed if not required.
AUTHOR_NAME: Final[str] = "oxeign"
AUTHOR_HASH: Final[str] = "c5c8cd48384b065a0e46d27016b4e3ea5c9a52bd12d87cd681bd426c480cce3a"
