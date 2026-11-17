"""Provider adapter interface.

Adapters only read **documented or locally stored, user-owned logs** of a provider. They never call provider
APIs, scrape private internals or extract hidden reasoning (``thinking`` blocks are always discarded).
Every adapter declares honest :class:`Capabilities`; features a provider does not expose are ``False``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


class UnsupportedOperation(RuntimeError):
    """Raised when an adapter cannot do something because the provider does not expose it."""


@dataclass(frozen=True)
class Capabilities:
    captures_prompts: bool = False
    captures_responses: bool = False
    captures_tool_calls: bool = False
    captures_file_edits: bool = False
    mechanism: str = ""  # how data is obtained
    verified_against_real_install: bool = False
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class ToolCall:
    id: str
    name: str
    kind: str  # write | edit | shell | read | other
    summary: str = ""  # short description (command / path)
    file_path: str | None = None
    ok: bool | None = None
    output_excerpt: str = ""


@dataclass
class FileEdit:
    path: str  # absolute path as recorded by the assistant
    kind: str  # create | modify
    content: str | None = None  # full content for Write
    edits: list[tuple[str, str]] = field(default_factory=list)  # (old, new) for Edit/MultiEdit
    timestamp: datetime | None = None
    ok: bool = True


@dataclass
class ShellCommand:
    command: str
    ok: bool | None
    is_test: bool = False
    output_excerpt: str = ""


@dataclass
class CapturedMessage:
    role: str  # user | assistant
    text: str
    timestamp: datetime | None = None
    model: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class ExternalSession:
    provider: str
    external_id: str
    title: str = ""
    cwd: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    source_path: str = ""
    message_count: int = 0
    git_branch: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        for k in ("started_at", "ended_at"):
            d[k] = d[k].isoformat() if d[k] else None
        return d


@dataclass
class CapturedSession:
    meta: ExternalSession
    messages: list[CapturedMessage] = field(default_factory=list)
    file_edits: list[FileEdit] = field(default_factory=list)
    commands: list[ShellCommand] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)  # e.g. {"thinking_blocks": 12}


class ProviderAdapter(ABC):
    key: str
    display_name: str
    kind: str = "cloud_assistant"
    capabilities: Capabilities = Capabilities()
    vscode_extension_ids: tuple[str, ...] = ()  # lets the extension report whether the assistant is installed

    @abstractmethod
    def available(self) -> bool:
        """True if this adapter's data source exists on this machine."""

    def discover(self, project_root: Path | None = None) -> list[ExternalSession]:
        raise UnsupportedOperation(f"{self.display_name} does not expose a readable session log")

    def load(self, ref: ExternalSession) -> CapturedSession:
        raise UnsupportedOperation(f"{self.display_name} does not expose a readable session log")

    def describe(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "display_name": self.display_name,
            "kind": self.kind,
            "capabilities": self.capabilities.to_dict(),
            "available": self.available(),
            "vscode_extension_ids": list(self.vscode_extension_ids),
        }
