"""Agent interface, execution context and result types."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.security.permissions import PermissionPolicy


@dataclass
class AgentResult:
    status: str  # success | failed | blocked | skipped
    summary: str = ""
    output: dict[str, Any] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "summary": self.summary, "output": self.output, "issues": self.issues}


@dataclass
class AgentContext:
    svc: Any  # app.services.container.Services
    task_id: int | None
    project_id: int | None
    project_root: Path | None
    policy: PermissionPolicy | None
    request: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    results: dict[str, AgentResult] = field(default_factory=dict)  # results of completed steps by step key
    attempt: int = 1

    def emit(self, event: str, data: dict[str, Any] | None = None) -> None:
        self.svc.events.publish(event, {"task_id": self.task_id, **(data or {})})

    def require_root(self) -> Path:
        if self.project_root is None or self.policy is None:
            raise ValueError("this agent needs a project")
        return self.project_root


class Agent:
    key: str = ""
    name: str = ""
    category: str = "development"  # development | testing | orchestration
    description: str = ""
    permissions: tuple[str, ...] = ()

    async def run(self, ctx: AgentContext, payload: dict[str, Any]) -> AgentResult:  # pragma: no cover - interface
        raise NotImplementedError
