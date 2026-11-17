"""Claude Code adapter: reads the user's own local session transcripts (JSONL).

Location: ``$CLAUDE_CONFIG_DIR`` or ``~/.claude`` → ``projects/<project path with non-alphanumerics → "-">/<session>.jsonl``.
Parsed (verified against a real transcript, 2026-11): ``user``/``assistant`` lines whose ``message.content`` is a
string or a list of ``text`` / ``thinking`` / ``tool_use`` / ``tool_result`` blocks.

Deliberately NOT captured: ``thinking`` blocks (hidden reasoning), ``<system-reminder>`` injections, API-request
blobs, attachments and any other bookkeeping line types.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.learning.adapters.base import (
    Capabilities,
    CapturedMessage,
    CapturedSession,
    ExternalSession,
    FileEdit,
    ProviderAdapter,
    ShellCommand,
    ToolCall,
)

_SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_NON_PROMPT_PREFIXES = ("<command-name>", "<command-message>", "<local-command", "Caveat:", "[Request interrupted")
_TEST_CMD = re.compile(
    r"\b(pytest|py\.test|unittest|vitest|jest|mocha|phpunit|pest|go test|cargo test|npm (run )?test|yarn test|pnpm (run )?test|composer test|artisan test|rspec|mvn test|gradle test|dotnet test)\b"
)


_EPOCH = datetime.fromtimestamp(0, UTC)  # sort key for sessions without timestamps


def encode_project_dir(path: str | Path) -> str:
    """Claude Code's directory naming: every non-alphanumeric character becomes ``-``."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _user_text(content: Any) -> str:
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    else:
        return ""
    text = _SYSTEM_REMINDER.sub("", text).strip()
    if not text or text.startswith(_NON_PROMPT_PREFIXES):
        return ""
    return text


class ClaudeCodeAdapter(ProviderAdapter):
    key = "claude-code"
    display_name = "Claude Code"
    vscode_extension_ids = ("anthropic.claude-code",)
    capabilities = Capabilities(
        captures_prompts=True,
        captures_responses=True,
        captures_tool_calls=True,
        captures_file_edits=True,
        mechanism="reads the user's local session transcripts (JSONL) after the fact",
        verified_against_real_install=True,
        notes="Hidden reasoning ('thinking' blocks) is discarded by design. Transcript format is not a public "
        "contract and may change between Claude Code versions; unknown line types are ignored.",
    )

    def __init__(self, config_dir: Path | None = None) -> None:
        env = os.environ.get("CLAUDE_CONFIG_DIR")
        self.config_dir = Path(config_dir or env or Path.home() / ".claude")

    @property
    def projects_dir(self) -> Path:
        return self.config_dir / "projects"

    def available(self) -> bool:
        return self.projects_dir.is_dir()

    def discover(self, project_root: Path | None = None) -> list[ExternalSession]:
        if not self.projects_dir.is_dir():
            return []
        dirs = (
            [self.projects_dir / encode_project_dir(project_root.resolve())]
            if project_root
            else [d for d in self.projects_dir.iterdir() if d.is_dir()]
        )
        out: list[ExternalSession] = []
        for d in dirs:
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.jsonl")):
                meta = self._scan(f)
                if meta:
                    out.append(meta)
        out.sort(key=lambda s: s.started_at or _EPOCH, reverse=True)
        return out

    def _scan(self, path: Path) -> ExternalSession | None:
        sid, cwd, branch, title = path.stem, None, None, ""
        first: datetime | None = None
        last: datetime | None = None
        count = 0
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        o = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(o, dict):
                        continue
                    t = o.get("type")
                    if t == "ai-title" and o.get("aiTitle"):
                        title = str(o["aiTitle"])
                    if t in ("user", "assistant"):
                        count += 1
                        cwd = cwd or o.get("cwd")
                        branch = branch or o.get("gitBranch")
                        sid = o.get("sessionId") or sid
                        ts = _ts(o.get("timestamp"))
                        if ts:
                            first = first or ts
                            last = ts
        except OSError:
            return None
        if count == 0:
            return None
        return ExternalSession(self.key, str(sid), title, cwd, first, last, str(path), count, branch)

    def load(self, ref: ExternalSession) -> CapturedSession:
        session = CapturedSession(meta=ref)
        calls: dict[str, tuple[ToolCall, FileEdit | None, ShellCommand | None]] = {}
        assistant_by_msg: dict[str, CapturedMessage] = {}
        skipped = {"thinking_blocks": 0, "system_injections": 0, "non_message_lines": 0, "sidechain_text": 0}
        with Path(ref.source_path).open(encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                try:
                    o = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(o, dict):
                    continue
                typ = o.get("type")
                msg = o.get("message")
                if typ not in ("user", "assistant") or not isinstance(msg, dict):
                    skipped["non_message_lines"] += 1
                    continue
                content = msg.get("content")
                ts = _ts(o.get("timestamp"))
                sidechain = bool(o.get("isSidechain"))
                if typ == "user":
                    if isinstance(content, list):
                        for b in content:
                            if isinstance(b, dict) and b.get("type") == "tool_result":
                                self._apply_result(calls, b)
                    if sidechain or o.get("isMeta"):
                        continue
                    text = _user_text(content)
                    raw_text = content if isinstance(content, str) else ""
                    if not text:
                        if raw_text or _has_text_block(content):
                            skipped["system_injections"] += 1
                        continue
                    session.messages.append(CapturedMessage("user", text, ts))
                    continue
                # assistant
                mid = str(msg.get("id") or o.get("uuid") or len(session.messages))
                cm = assistant_by_msg.get(mid)
                if cm is None:
                    cm = CapturedMessage("assistant", "", ts, msg.get("model"))
                    assistant_by_msg[mid] = cm
                    session.messages.append(cm)
                if not isinstance(content, list):
                    continue
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "thinking" or bt == "redacted_thinking":
                        skipped["thinking_blocks"] += 1
                    elif bt == "text":
                        if sidechain:
                            skipped["sidechain_text"] += 1
                        else:
                            cm.text = (cm.text + "\n\n" + b.get("text", "")).strip()
                    elif bt == "tool_use":
                        self._register_call(session, cm, calls, b, ts)
        session.messages = [m for m in session.messages if m.text or m.tool_calls]
        session.skipped = skipped
        return session

    @staticmethod
    def _register_call(
        session: CapturedSession,
        cm: CapturedMessage,
        calls: dict[str, tuple[ToolCall, FileEdit | None, ShellCommand | None]],
        block: dict[str, Any],
        ts: datetime | None,
    ) -> None:
        name = str(block.get("name", ""))
        inp: dict[str, Any] = block["input"] if isinstance(block.get("input"), dict) else {}
        cid = str(block.get("id", ""))
        edit: FileEdit | None = None
        shell: ShellCommand | None = None
        if name == "Write":
            fp = str(inp.get("file_path", ""))
            call = ToolCall(cid, name, "write", fp, fp)
            edit = FileEdit(fp, "modify", content=str(inp.get("content", "")), timestamp=ts)
        elif name in ("Edit", "MultiEdit"):
            fp = str(inp.get("file_path", ""))
            call = ToolCall(cid, name, "edit", fp, fp)
            pairs = (
                [(str(inp.get("old_string", "")), str(inp.get("new_string", "")))]
                if name == "Edit"
                else [
                    (str(e.get("old_string", "")), str(e.get("new_string", "")))
                    for e in inp.get("edits", [])
                    if isinstance(e, dict)
                ]
            )
            edit = FileEdit(fp, "modify", edits=pairs, timestamp=ts)
        elif name == "Bash":
            cmd = str(inp.get("command", ""))
            call = ToolCall(cid, name, "shell", cmd[:300])
            shell = ShellCommand(cmd, None, bool(_TEST_CMD.search(cmd)))
        else:
            call = ToolCall(
                cid,
                name,
                "read" if name in ("Read", "Grep", "Glob", "LS") else "other",
                str(inp.get("file_path") or inp.get("pattern") or "")[:200],
            )
        cm.tool_calls.append(call)
        calls[cid] = (call, edit, shell)
        if edit:
            session.file_edits.append(edit)
        if shell:
            session.commands.append(shell)

    @staticmethod
    def _apply_result(
        calls: dict[str, tuple[ToolCall, FileEdit | None, ShellCommand | None]], block: dict[str, Any]
    ) -> None:
        entry = calls.get(str(block.get("tool_use_id", "")))
        if not entry:
            return
        call, edit, shell = entry
        text = _result_text(block.get("content"))
        ok = not bool(block.get("is_error"))
        call.ok = ok
        call.output_excerpt = text[:400]
        if edit is not None:
            edit.ok = ok
            if call.name == "Write" and ok:
                edit.kind = "create" if "created successfully" in text else "modify"
        if shell is not None:
            shell.ok = ok
            shell.output_excerpt = text[:600]


def _has_text_block(content: Any) -> bool:
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "text" for b in content)
