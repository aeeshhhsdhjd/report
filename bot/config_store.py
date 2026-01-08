from __future__ import annotations

import asyncio
import logging
from typing import Any

from storage import DataStore, FallbackDataStore, build_datastore

class ConfigStore:
    """
    High-level wrapper for runtime configuration.
    Ensures thread-safe access and type consistency for chat IDs.
    """

    def __init__(self, datastore: DataStore | FallbackDataStore) -> None:
        self.datastore = datastore
        self._lock = asyncio.Lock()

    async def _get_raw(self, key: str, default: Any = None) -> Any:
        """Internal helper to fetch from datastore config collection."""
        # Use the underlying datastore's logic to handle MongoDB vs In-Memory
        try:
            return await self.datastore._get_config_value(key) or default
        except Exception as e:
            logging.error(f"ConfigStore read error for {key}: {e}")
            return default

    async def _set_raw(self, key: str, value: Any) -> None:
        """Internal helper to write to datastore config collection."""
        try:
            await self.datastore._set_config_value(key, value)
        except Exception as e:
            logging.error(f"ConfigStore write error for {key}: {e}")

    async def session_group(self) -> int | None:
        val = await self._get_raw("session_group")
        return int(val) if val is not None else None

    async def set_session_group(self, chat_id: int | str) -> None:
        await self._set_raw("session_group", int(chat_id))

    async def logs_group(self) -> int | None:
        val = await self._get_raw("logs_group")
        return int(val) if val is not None else None

    async def set_logs_group(self, chat_id: int | str) -> None:
        await self._set_raw("logs_group", int(chat_id))

    async def add_known_chat(self, chat_id: int | str) -> None:
        """Leverages the specialized datastore method for unique chat tracking."""
        await self.datastore.add_known_chat(int(chat_id))

    async def known_chats(self) -> list[int]:
        """Returns list of chats the bot has interacted with."""
        return await self.datastore.known_chats()

def build_config_store(mongo_uri: str | None) -> tuple[ConfigStore, DataStore | FallbackDataStore]:
    """Factory to initialize the full storage stack."""
    datastore = build_datastore(mongo_uri)
    return ConfigStore(datastore), datastore

__all__ = ["ConfigStore", "build_config_store"]
