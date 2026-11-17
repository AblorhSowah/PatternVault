"""In-process event bus feeding the WebSocket hub (and the orchestrator's progress reporting)."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.models.base import utcnow


class EventBus:
    """Thread-safe publish, asyncio subscribe. Keeps a short replay buffer for late subscribers."""

    def __init__(self, history: int = 200) -> None:
        self._subs: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]] = set()
        self._lock = threading.Lock()
        self._history: deque[dict[str, Any]] = deque(maxlen=history)
        self._seq = 0

    def publish(
        self, event_type: str, data: dict[str, Any] | None = None, *, ephemeral: bool = False
    ) -> dict[str, Any]:
        """Deliver to live subscribers. ``ephemeral`` events (e.g. streamed chat tokens) skip the replay buffer so
        they cannot push meaningful events out of it."""
        with self._lock:
            self._seq += 1
            event = {"seq": self._seq, "type": event_type, "ts": utcnow().isoformat(), "data": data or {}}
            if not ephemeral:
                self._history.append(event)
            subs = list(self._subs)
        for loop, queue in subs:
            try:
                loop.call_soon_threadsafe(self._put, queue, event)
            except RuntimeError:  # loop closed
                self._discard(loop, queue)
        return event

    @staticmethod
    def _put(queue: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
        if queue.full():  # drop the oldest rather than block the publisher
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
        queue.put_nowait(event)

    def _discard(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subs.discard((loop, queue))

    @contextmanager
    def subscribe(self, replay: bool = False) -> Iterator[asyncio.Queue[dict[str, Any]]]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        with self._lock:
            if replay:
                for e in self._history:
                    queue.put_nowait(e)
            self._subs.add((loop, queue))
        try:
            yield queue
        finally:
            self._discard(loop, queue)

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._history)[-limit:]
