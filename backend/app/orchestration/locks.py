"""Write-serialisation: per-path locks (within a task) and a per-project mutation lock (across tasks)."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class FileLockManager:
    def __init__(self) -> None:
        self._path_locks: dict[tuple[int, str], asyncio.Lock] = defaultdict(asyncio.Lock)
        self._project_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.holders: dict[tuple[int, str], str] = {}

    @asynccontextmanager
    async def paths(self, project_id: int, paths: list[str], owner: str) -> AsyncIterator[None]:
        """Acquire every path lock in sorted order (deadlock-free); concurrent writers of the same file are serialised."""
        keys = sorted({(project_id, p) for p in paths})
        acquired: list[tuple[int, str]] = []
        try:
            for k in keys:
                await self._path_locks[k].acquire()
                self.holders[k] = owner
                acquired.append(k)
            yield
        finally:
            for k in reversed(acquired):
                self.holders.pop(k, None)
                self._path_locks[k].release()

    @asynccontextmanager
    async def project(self, project_id: int) -> AsyncIterator[None]:
        """Only one task at a time may run the mutate→test→fix cycle on a project."""
        async with self._project_locks[project_id]:
            yield

    def is_locked(self, project_id: int, path: str) -> bool:
        return self._path_locks[(project_id, path)].locked()
