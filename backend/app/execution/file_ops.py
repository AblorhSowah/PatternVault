"""Permission-gated, atomic, recoverable file changes."""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.execution.recovery import RecoveryManager
from app.generation.generator import unified_diff
from app.orchestration.approvals import ApprovalService
from app.security.permissions import Decision, PermissionPolicy


class ChangeDenied(RuntimeError):
    def __init__(self, denied: list[dict[str, str]]) -> None:
        super().__init__("; ".join(f"{d['path']}: {d['reason']}" for d in denied))
        self.denied = denied


@dataclass
class ChangeSpec:
    path: str
    content: str | None  # None = delete
    action: str = "modify"  # create | modify | delete


@dataclass
class ApplyResult:
    applied: list[str]
    recovery_point_id: int | None
    approvals: list[int] = field(default_factory=list)
    created: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class FileOps:
    def __init__(self, recovery: RecoveryManager, approvals: ApprovalService, audit: Any, events: Any) -> None:
        self._recovery = recovery
        self._approvals = approvals
        self._audit = audit
        self._events = events

    def evaluate(self, policy: PermissionPolicy, changes: list[ChangeSpec]) -> list[tuple[ChangeSpec, Decision, str]]:
        out = []
        for c in changes:
            if c.content is None:
                v = policy.check_delete(c.path)
            else:
                real, _ = policy.resolve_inside(c.path)
                v = policy.check_write(c.path, creating=not (real is not None and real.exists()))
            out.append((c, v.decision, v.reason))
        return out

    async def apply(
        self,
        project_id: int,
        policy: PermissionPolicy,
        changes: list[ChangeSpec],
        *,
        task_id: int | None = None,
        label: str = "",
        actor: str = "agent:execution",
        approval_timeout_s: float = 3600.0,
    ) -> ApplyResult:
        """Evaluate every change first; DENY aborts the whole batch before anything is touched; approvals are awaited;
        a recovery point is taken; writes are atomic; any failure restores the recovery point."""
        verdicts = self.evaluate(policy, changes)
        denied = [{"path": c.path, "reason": reason} for c, d, reason in verdicts if d is Decision.DENY]
        if denied:
            self._audit.record(
                actor,
                "files.apply",
                resource_type="project",
                resource_id=project_id,
                outcome="denied",
                permission_level=policy.level.value,
                detail={"denied": denied},
            )
            raise ChangeDenied(denied)
        approval_ids: list[int] = []
        for c, d, reason in verdicts:
            if d is Decision.REQUIRE_APPROVAL:
                preview = "(delete)" if c.content is None else c.content[:1500]
                approval_ids.append(
                    self._approvals.request(
                        task_id,
                        "write_file" if c.content is not None else "delete_file",
                        f"{c.action} {c.path}: {reason}",
                        {"path": c.path, "reason": reason, "preview": preview},
                        requested_by=actor,
                    )
                )
        for aid in approval_ids:
            await self._approvals.wait(aid, approval_timeout_s)
        root = policy.project_root
        point = self._recovery.create(
            project_id, root, [c.path for c in changes], task_id=task_id, label=label or "before apply"
        )
        applied: list[str] = []
        created: list[str] = []
        try:
            for c in changes:
                real, why = policy.resolve_inside(c.path)
                if real is None:
                    raise ChangeDenied([{"path": c.path, "reason": why}])
                if c.content is None:
                    real.unlink(missing_ok=True)
                else:
                    existed = real.exists()
                    real.parent.mkdir(parents=True, exist_ok=True)
                    self._atomic_write(real, c.content, real.stat().st_mode & 0o777 if existed else None)
                    if not existed:
                        created.append(c.path)
                        policy.created_paths.add(policy.rel(real))
                applied.append(c.path)
                self._audit.record(
                    actor,
                    "file.delete" if c.content is None else "file.write",
                    resource_type="file",
                    resource_id=c.path,
                    permission_level=policy.level.value,
                    detail={"task_id": task_id, "recovery_point": point},
                )
        except BaseException:
            self._recovery.restore(point, root)
            self._audit.record(
                actor, "files.apply.rollback", resource_type="recovery_point", resource_id=point, outcome="error"
            )
            raise
        self._events.publish("files.applied", {"task_id": task_id, "files": applied, "recovery_point_id": point})
        return ApplyResult(applied, point, approval_ids, created)

    @staticmethod
    def _atomic_write(path: Path, text: str, mode: int | None) -> None:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".pv-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
            if mode is not None:
                os.chmod(tmp, mode)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp).unlink()
            raise

    @staticmethod
    def preview(policy: PermissionPolicy, changes: list[ChangeSpec]) -> list[dict[str, str]]:
        out = []
        for c in changes:
            real, _ = policy.resolve_inside(c.path)
            before = real.read_text(encoding="utf-8", errors="replace") if real is not None and real.is_file() else ""
            out.append({"path": c.path, "diff": unified_diff(c.path, before, c.content or "")})
        return out
