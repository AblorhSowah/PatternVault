"""Policy-gated command execution: sanitised environment, timeouts (process-group kill), output caps + redaction."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.orchestration.approvals import ApprovalService
from app.security.permissions import Decision, PermissionPolicy, command_text
from app.security.secrets import redact

MAX_OUTPUT = 400_000
_SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE|SESSION)", re.I)
_KEEP_ENV = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TERM",
    "USER",
    "LOGNAME",
    "SHELL",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "VIRTUAL_ENV",
    "NODE_PATH",
    "PLAYWRIGHT_BROWSERS_PATH",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "UV_CACHE_DIR",
    "UV_PYTHON_INSTALL_DIR",
)


class CommandDenied(RuntimeError):
    pass


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    truncated: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def sanitized_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Pass through only what a build/test run needs; secrets and PatternVault's own variables never reach children."""
    env = {k: v for k, v in os.environ.items() if k in _KEEP_ENV and not _SECRET_ENV.search(k)}
    env.update(
        {"CI": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", "NO_COLOR": "1", "FORCE_COLOR": "0"}
    )
    for k, v in (extra or {}).items():
        env[k] = v
    return env


class CommandExecutor:
    def __init__(self, approvals: ApprovalService, audit: Any, events: Any) -> None:
        self._approvals = approvals
        self._audit = audit
        self._events = events

    async def run(
        self,
        argv: list[str],
        cwd: Path,
        policy: PermissionPolicy,
        *,
        timeout_s: float = 600.0,
        task_id: int | None = None,
        env: dict[str, str] | None = None,
        actor: str = "agent:execution",
        pre_approved: bool = False,
        approval_timeout_s: float = 3600.0,
    ) -> CommandResult:
        text = command_text(argv)
        verdict = policy.check_command(argv)
        if verdict.decision is Decision.DENY:
            self._audit.record(
                actor,
                "command.run",
                resource_type="command",
                outcome="denied",
                permission_level=policy.level.value,
                detail={"command": text[:300], "reason": verdict.reason, "rule": verdict.rule},
            )
            raise CommandDenied(f"{verdict.reason} [{verdict.rule}]")
        if verdict.decision is Decision.REQUIRE_APPROVAL and not pre_approved:
            aid = self._approvals.request(
                task_id,
                "run_command",
                f"Run: {text[:200]} ({verdict.reason})",
                {"command": redact(text).text[:500], "cwd": str(cwd), "reason": verdict.reason},
                requested_by=actor,
            )
            await self._approvals.wait(aid, approval_timeout_s)
        real, why = policy.resolve_inside(cwd)
        if real is None:
            raise CommandDenied(f"working directory rejected: {why}")
        t0 = time.monotonic()
        self._events.publish("command.started", {"task_id": task_id, "command": redact(text).text[:200]})
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(real),
            env=sanitized_env(env),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        timed_out = False
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            timed_out = True
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            out_b, err_b = await proc.communicate()
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            raise
        truncated = len(out_b) > MAX_OUTPUT or len(err_b) > MAX_OUTPUT
        res = CommandResult(
            argv,
            proc.returncode,
            redact(out_b[-MAX_OUTPUT:].decode("utf-8", "replace")).text,
            redact(err_b[-MAX_OUTPUT:].decode("utf-8", "replace")).text,
            time.monotonic() - t0,
            timed_out,
            truncated,
        )
        self._audit.record(
            actor,
            "command.run",
            resource_type="command",
            permission_level=policy.level.value,
            detail={
                "command": redact(text).text[:300],
                "rc": res.returncode,
                "duration_s": round(res.duration_s, 2),
                "timed_out": timed_out,
                "task_id": task_id,
            },
        )
        return res
