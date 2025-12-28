"""Async storage helpers for session strings and report summaries."""
from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Iterable


class DataStore:
    """Persist session strings and report audit records."""

    def __init__(
        self,
        client,
        db,
        *,
        mongo_uri: str | None = None,
        db_name: str = "reporter",
        mongo_env_var: str = "MONGO_URI",
    ) -> None:
        self.mongo_env_var = mongo_env_var
        self.mongo_uri = mongo_uri or os.getenv(self.mongo_env_var, "")
        self._in_memory_sessions: set[str] = set()
        self._in_memory_reports: list[dict] = []

        self.client = client
        self.db = db or (self.client.get_default_database() if self.client else None)
        if not self.db and self.client:
            self.db = self.client[db_name]

    async def add_sessions(self, sessions: Iterable[str], added_by: int | None = None) -> list[str]:
        """Add unique session strings and return the list that were newly stored."""

        added: list[str] = []
        normalized = [s.strip() for s in sessions if s and s.strip()]

        if self.db:
            for session in normalized:
                result = await self.db.sessions.update_one(
                    {"session": session},
                    {
                        "$setOnInsert": {
                            "created_at": dt.datetime.utcnow(),
                            "added_by": added_by,
                        }
                    },
                    upsert=True,
                )
                if result.upserted_id:
                    added.append(session)
        else:
            for session in normalized:
                if session not in self._in_memory_sessions:
                    self._in_memory_sessions.add(session)
                    added.append(session)

        return added

    async def get_sessions(self) -> list[str]:
        """Return all known session strings."""

        if self.db:
            cursor = self.db.sessions.find({}, {"_id": False, "session": True})
            return [doc["session"] async for doc in cursor]

        return list(self._in_memory_sessions)

    async def record_report(self, payload: dict) -> None:
        """Persist a report summary payload."""

        payload = {
            **payload,
            "stored_at": dt.datetime.utcnow(),
        }
        if self.db:
            await self.db.reports.insert_one(payload)
        else:
            self._in_memory_reports.append(payload)

    async def remove_sessions(self, sessions: Iterable[str]) -> int:
        """Remove sessions from persistence, returning the count removed."""

        targets = {s for s in sessions if s}
        if not targets:
            return 0

        removed = 0
        if self.db:
            result = await self.db.sessions.delete_many({"session": {"$in": list(targets)}})
            removed = getattr(result, "deleted_count", 0)
        else:
            for session in list(targets):
                if session in self._in_memory_sessions:
                    self._in_memory_sessions.discard(session)
                    removed += 1

        return removed

    async def close(self) -> None:
        if self.client:
            self.client.close()

    @property
    def is_persistent(self) -> bool:
        """Expose whether MongoDB is available for callers that want to log mode."""

        return bool(self.db)


class FallbackDataStore:
    """In-memory persistence used when MongoDB is unavailable."""

    def __init__(self) -> None:
        self._in_memory_sessions: set[str] = set()
        self._in_memory_reports: list[dict] = []
        self.client = None
        self.db = None

    async def add_sessions(self, sessions: Iterable[str], added_by: int | None = None) -> list[str]:
        added: list[str] = []
        normalized = [s.strip() for s in sessions if s and s.strip()]
        for session in normalized:
            if session not in self._in_memory_sessions:
                self._in_memory_sessions.add(session)
                added.append(session)
        return added

    async def get_sessions(self) -> list[str]:
        return list(self._in_memory_sessions)

    async def record_report(self, payload: dict) -> None:
        payload = {
            **payload,
            "stored_at": dt.datetime.utcnow(),
        }
        self._in_memory_reports.append(payload)

    async def remove_sessions(self, sessions: Iterable[str]) -> int:
        targets = {s for s in sessions if s}
        removed = 0
        for session in list(targets):
            if session in self._in_memory_sessions:
                self._in_memory_sessions.discard(session)
                removed += 1
        return removed

    async def close(self) -> None:  # pragma: no cover - symmetry with DataStore
        return None

    @property
    def is_persistent(self) -> bool:
        return False


def build_datastore(
    mongo_uri: str | None,
    *,
    db_name: str = "reporter",
    mongo_env_var: str = "MONGO_URI",
) -> DataStore | FallbackDataStore:
    """Build a datastore safely, falling back when MongoDB is unavailable."""

    resolved_uri = mongo_uri or os.getenv(mongo_env_var, "")
    if not resolved_uri:
        logging.warning(
            "MongoDB persistence disabled; set %s to a MongoDB connection URI to enable it.",
            mongo_env_var,
        )
        return FallbackDataStore()

    try:  # pragma: no cover - optional dependency
        import motor.motor_asyncio as motor_asyncio
    except Exception as exc:
        logging.warning(
            "MongoDB URI provided but Motor is unavailable; falling back to in-memory storage. Import error: %s",
            exc,
        )
        return FallbackDataStore()

    try:
        client = motor_asyncio.AsyncIOMotorClient(resolved_uri)
        db = client.get_default_database() or client[db_name]
        logging.info("Connected to MongoDB for session persistence.")
        return DataStore(client, db, mongo_uri=resolved_uri, db_name=db_name, mongo_env_var=mongo_env_var)
    except Exception as exc:
        logging.warning(
            "Failed to initialize MongoDB client with %s; falling back to in-memory storage: %s",
            mongo_env_var,
            exc,
        )
        return FallbackDataStore()
