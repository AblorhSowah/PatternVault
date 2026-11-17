"""Turns a request into an ordered list of steps (a small DAG)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agents.registry import TESTING_AGENT_KEYS
from app.testing.workspace import TargetWorkspace

TASK_KINDS = ("generate", "replay", "debug", "review", "test", "document")


@dataclass
class StepSpec:
    key: str
    kind: str  # agent | apply | verify | learn
    agent: str | None = None
    depends: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    optional: bool = False
    always: bool = False  # run once dependencies have *finished*, even if they failed (e.g. to record failed outcomes)

    def to_plan(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "agent": self.agent,
            "depends": self.depends,
            "status": "pending",
            "summary": "",
        }


def choose_testing_agents(ws: TargetWorkspace | None, requested: list[str] | None) -> list[str]:
    if requested:
        return [k for k in requested if k in TESTING_AGENT_KEYS]
    if ws is None:
        return ["unit-testing"]
    keys = ["unit-testing"]
    if ws.tests_dir is not None and (ws.tests_dir / "integration").is_dir():
        keys.append("integration-testing")
    keys.append("code-quality")
    keys.append("security-testing")
    return keys


def plan(kind: str, params: dict[str, Any], ws: TargetWorkspace | None) -> list[StepSpec]:
    agents = choose_testing_agents(ws, params.get("agents"))
    verify = StepSpec("verify", "verify", payload={"agents": agents, "fix": bool(params.get("fix", True))})
    if kind == "generate":
        verify.depends.append("apply")
        return [
            StepSpec("context", "agent", "context-retrieval"),
            StepSpec("generate", "agent", "code-generation", ["context"]),
            StepSpec("review", "agent", "code-review-security", ["generate"]),
            StepSpec("apply", "apply", depends=["review"], payload={"source": "generate"}),
            verify,
            StepSpec("learn", "learn", depends=["verify"], payload={"source": "generate"}, always=True),
        ]
    if kind == "replay":
        steps = [
            StepSpec("replay", "agent", "pattern-replay"),
            StepSpec("review", "agent", "code-review-security", ["replay"]),
            StepSpec("apply", "apply", depends=["review"], payload={"source": "replay"}),
        ]
        verify.depends.append("apply")
        return [
            *steps,
            verify,
            StepSpec("learn", "learn", depends=["verify"], payload={"source": "replay"}, always=True),
        ]
    if kind == "debug":
        return [verify]
    if kind == "review":
        return [StepSpec("review", "agent", "code-review-security", payload={"files": params.get("files")})]
    if kind == "test":
        verify.payload["fix"] = bool(params.get("fix", False))
        return [verify]
    if kind == "document":
        return [
            StepSpec(
                "document",
                "agent",
                "documentation",
                payload={"kind": params.get("doc_kind", "all"), "write": bool(params.get("write", False))},
            )
        ]
    raise ValueError(f"unknown task kind '{kind}'")
