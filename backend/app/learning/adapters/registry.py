"""Adapter registry."""

from __future__ import annotations

from pathlib import Path

from app.learning.adapters.base import ProviderAdapter
from app.learning.adapters.claude_code import ClaudeCodeAdapter
from app.learning.adapters.continue_dev import ContinueAdapter
from app.learning.adapters.unsupported import CopilotAdapter, CursorAdapter, ManualAdapter


class AdapterRegistry:
    def __init__(self, claude_dir: Path | None = None, continue_dir: Path | None = None) -> None:
        adapters: list[ProviderAdapter] = [
            ClaudeCodeAdapter(claude_dir),
            ContinueAdapter(continue_dir),
            CopilotAdapter(),
            CursorAdapter(),
            ManualAdapter(),
        ]
        self._by_key = {a.key: a for a in adapters}

    def get(self, key: str) -> ProviderAdapter:
        try:
            return self._by_key[key]
        except KeyError as exc:
            raise KeyError(f"unknown provider '{key}'") from exc

    def all(self) -> list[ProviderAdapter]:
        return list(self._by_key.values())
