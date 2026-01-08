from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from time import monotonic
from typing import Awaitable, Callable, Dict, Optional


@dataclass
class UserState:
    """Conversation state for a user."""
    user_id: Optional[int] = None  # Track the user this state belongs to
    stage: str = "idle"
    report_type: Optional[str] = None
    target_link: Optional[str] = None
    reason_code: Optional[int] = None
    reason_text: Optional[str] = None
    report_count: Optional[int] = None
    next_stage_after_count: Optional[str] = None
    started_at: float = field(default_factory=monotonic)

    def reset(self) -> None:
        self.stage = "idle"
        self.report_type = None
        self.target_link = None
        self.reason_code = None
        self.reason_text = None
        self.report_count = None
        self.next_stage_after_count = None
        self.started_at = monotonic()


class StateManager:
    """Manage UserState instances keyed by user id."""

    def __init__(self) -> None:
        self._states: Dict[int, UserState] = {}

    def get(self, user_id: int) -> UserState:
        if user_id not in self._states:
            self._states[user_id] = UserState(user_id=user_id)
        return self._states[user_id]

    def reset(self, user_id: int) -> None:
        if user_id in self._states:
            self._states[user_id].reset()
        else:
            self._states[user_id] = UserState(user_id=user_id)


class QueueEntry:
    """Item waiting to be processed by the sequential queue."""

    def __init__(
        self,
        user_id: int,
        job: Callable[[], Awaitable[None]],
        notify_position: Optional[Callable[[int], Awaitable[None]]] = None,
    ) -> None:
        self.user_id = user_id
        self.job = job
        self.notify_position = notify_position


class ReportQueue:
    """FIFO queue ensuring only one report executes at a time."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[QueueEntry] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._active_user: Optional[int] = None
        self._on_error: Optional[Callable[[Exception], Awaitable[None]]] = None

    @property
    def active_user(self) -> Optional[int]:
        return self._active_user

    def set_error_handler(self, handler: Callable[[Exception], Awaitable[None]]) -> None:
        self._on_error = handler

    def expected_position(self, user_id: int) -> int:
        """Calculate the 1-based position for a new user entering the queue."""
        # Include current active job in count
        q_size = self._queue.qsize()
        active_offset = 1 if self._active_user is not None else 0
        return q_size + active_offset + 1

    async def enqueue(self, entry: QueueEntry) -> int:
        position = self.expected_position(entry.user_id)
        await self._queue.put(entry)
        
        if entry.notify_position:
            await entry.notify_position(position)
            
        # Ensure worker is running
        if not self._worker_task or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._run_worker())
            
        return position

    async def _run_worker(self) -> None:
        """Worker loop that pulls from the queue."""
        while True:
            try:
                # Wait for next job with a timeout to allow the task to close if idle
                entry = await asyncio.wait_for(self._queue.get(), timeout=60.0)
                self._active_user = entry.user_id
                
                try:
                    await entry.job()
                except Exception as exc:
                    logging.exception(f"Job failed for user {entry.user_id}")
                    if self._on_error:
                        await self._on_error(exc)
                finally:
                    self._active_user = None
                    self._queue.task_done()
                    
            except asyncio.TimeoutError:
                # No jobs for 60s, shut down worker to save resources
                break
            except Exception:
                logging.exception("Fatal queue worker error")
                break

    def is_busy(self) -> bool:
        return self._active_user is not None or not self._queue.empty()
