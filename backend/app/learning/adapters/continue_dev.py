"""Continue (continue.dev) adapter: reads the chat sessions Continue stores locally.

Documented location: ``~/.continue/sessions/<sessionId>.json`` containing ``sessionId``, ``title``,
``workspaceDirectory`` and ``history[].message.{role,content}``.

HONEST STATUS: implemented from Continue's documented on-disk format and tested only against synthetic
fixtures — there was no Continue installation on the development machine, so
``verified_against_real_install`` is False. Continue's chat log does not record file edits; file changes come
from workspace observation.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.learning.adapters.base import (
    Capabilities,
    CapturedMessage,
    CapturedSession,
    ExternalSession,
    ProviderAdapter,
)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


class ContinueAdapter(ProviderAdapter):
    key = "continue"
    display_name = "Continue"
    vscode_extension_ids = ("continue.continue",)
    capabilities = Capabilities(
        captures_prompts=True,
        captures_responses=True,
        captures_tool_calls=False,
        captures_file_edits=False,
        mechanism="reads Continue's locally stored chat sessions (~/.continue/sessions)",
        verified_against_real_install=False,
        notes="Implemented from the documented session format; not yet verified against a real Continue install.",
    )

    def __init__(self, continue_dir: Path | None = None) -> None:
        env = os.environ.get("CONTINUE_GLOBAL_DIR")
        self.root = Path(continue_dir or env or Path.home() / ".continue")

    @property
    def sessions_dir(self) -> Path:
        return self.root / "sessions"

    def available(self) -> bool:
        return self.sessions_dir.is_dir()

    def discover(self, project_root: Path | None = None) -> list[ExternalSession]:
        if not self.sessions_dir.is_dir():
            return []
        out: list[ExternalSession] = []
        for f in sorted(self.sessions_dir.glob("*.json")):
            if f.name == "sessions.json":
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict) or not isinstance(data.get("history"), list):
                continue
            ws = data.get("workspaceDirectory")
            if project_root and ws and Path(str(ws).removeprefix("file://")).resolve() != project_root.resolve():
                continue
            mtime = datetime.fromtimestamp(f.stat().st_mtime, UTC)
            out.append(
                ExternalSession(
                    self.key,
                    str(data.get("sessionId") or f.stem),
                    str(data.get("title") or ""),
                    ws,
                    mtime,
                    mtime,
                    str(f),
                    len(data["history"]),
                )
            )
        return out

    def load(self, ref: ExternalSession) -> CapturedSession:
        data = json.loads(Path(ref.source_path).read_text(encoding="utf-8"))
        session = CapturedSession(meta=ref)
        for item in data.get("history", []):
            m = item.get("message") if isinstance(item, dict) else None
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            text = _content_text(m.get("content")).strip()
            if role in ("user", "assistant") and text:
                session.messages.append(
                    CapturedMessage(role, text, model=m.get("model") if role == "assistant" else None)
                )
        return session
