"""Agent registry: instances, persisted definitions and enable/disable state."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.agents.base import Agent
from app.agents.dev_agents import (
    CodeGenerationAgent,
    CodeReviewSecurityAgent,
    ContextRetrievalAgent,
    DebuggingAgent,
    DocumentationAgent,
    KnowledgeManagementAgent,
    LearningObservationAgent,
    PatternExtractionAgent,
    PatternReplayAgent,
)
from app.models import AgentDefinition, AgentExecution
from app.testing.agents.probes import ApiTestingAgent, DatabaseTestingAgent, ExtensionTestingAgent
from app.testing.agents.quality import (
    CodeQualityAgent,
    PerformanceTestingAgent,
    RegressionTestingAgent,
    SecurityTestingAgent,
)
from app.testing.agents.suites import EndToEndTestingAgent, IntegrationTestingAgent, UnitTestingAgent

TESTING_AGENT_KEYS = (
    "unit-testing",
    "integration-testing",
    "e2e-testing",
    "api-testing",
    "database-testing",
    "extension-testing",
    "security-testing",
    "performance-testing",
    "regression-testing",
    "code-quality",
)


class AgentRegistry:
    def __init__(self, svc: Any) -> None:
        self._svc = svc
        agents: list[Agent] = [
            LearningObservationAgent(),
            PatternExtractionAgent(),
            KnowledgeManagementAgent(),
            ContextRetrievalAgent(),
            CodeGenerationAgent(),
            PatternReplayAgent(),
            DebuggingAgent(),
            CodeReviewSecurityAgent(),
            DocumentationAgent(),
            UnitTestingAgent(),
            IntegrationTestingAgent(),
            EndToEndTestingAgent(),
            ApiTestingAgent(),
            DatabaseTestingAgent(),
            ExtensionTestingAgent(),
            SecurityTestingAgent(),
            PerformanceTestingAgent(),
            RegressionTestingAgent(),
            CodeQualityAgent(),
        ]
        self._agents = {a.key: a for a in agents}
        self.sync_definitions()

    def get(self, key: str) -> Agent:
        try:
            return self._agents[key]
        except KeyError as exc:
            raise KeyError(f"unknown agent '{key}'") from exc

    def all(self) -> list[Agent]:
        return list(self._agents.values())

    def is_enabled(self, key: str) -> bool:
        return key not in self._svc.config.config.agents.disabled

    def sync_definitions(self) -> None:
        with self._svc.session_factory() as db:
            existing = {d.key: d for d in db.execute(select(AgentDefinition)).scalars()}
            for a in self._agents.values():
                d = existing.get(a.key)
                if d is None:
                    db.add(
                        AgentDefinition(
                            key=a.key,
                            name=a.name,
                            category=a.category,
                            description=a.description,
                            permissions=list(a.permissions),
                        )
                    )
                else:
                    d.name, d.category, d.description, d.permissions = (
                        a.name,
                        a.category,
                        a.description,
                        list(a.permissions),
                    )
            if "orchestrator" not in existing:
                db.add(
                    AgentDefinition(
                        key="orchestrator",
                        name="Orchestrator Agent",
                        category="orchestration",
                        description="Plans requests into task graphs, coordinates agents, serialises file writes, enforces the verification gate.",
                        permissions=["coordinate_agents"],
                    )
                )
            db.commit()

    def describe(self) -> list[dict[str, Any]]:
        disabled = set(self._svc.config.config.agents.disabled)
        out = []
        with self._svc.session_factory() as db:
            for d in db.execute(
                select(AgentDefinition).order_by(AgentDefinition.category, AgentDefinition.key)
            ).scalars():
                last = (
                    db.execute(
                        select(AgentExecution)
                        .where(AgentExecution.agent_key == d.key)
                        .order_by(AgentExecution.id.desc())
                    )
                    .scalars()
                    .first()
                )
                running = (
                    db.query(AgentExecution)
                    .filter(AgentExecution.agent_key == d.key, AgentExecution.status == "running")
                    .count()
                )
                out.append(
                    {
                        "key": d.key,
                        "name": d.name,
                        "category": d.category,
                        "description": d.description,
                        "permissions": d.permissions,
                        "enabled": d.key not in disabled,
                        "status": "running" if running else "idle",
                        "config": d.config,
                        "last_execution": {
                            "id": last.id,
                            "task_id": last.task_id,
                            "status": last.status,
                            "step": last.step_key,
                            "duration_ms": last.duration_ms,
                            "at": last.started_at.isoformat(),
                        }
                        if last
                        else None,
                    }
                )
        return out
