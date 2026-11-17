"""Unit, integration and end-to-end testing agents (pytest / vitest / jest)."""

from __future__ import annotations

from typing import Any

from app.agents.base import AgentContext
from app.testing.agents.base import TestingAgent
from app.testing.report import TestReport
from app.testing.workspace import TargetWorkspace


class UnitTestingAgent(TestingAgent):
    key = "unit-testing"
    name = "Unit Testing Agent"
    description = (
        "Runs the project's unit tests (pytest, vitest or jest). Passes only if ≥1 test executed and none failed."
    )

    async def execute(self, ctx: AgentContext, ws: TargetWorkspace, payload: dict[str, Any]) -> TestReport:
        if ws.has_python_project and ws.tests_dir is not None:
            unit = ws.tests_dir / "unit"
            if unit.is_dir():
                return await self.pytest(ctx, ws, [str(unit.relative_to(ws.root))])
            return await self.pytest(
                ctx,
                ws,
                [str(ws.tests_dir.relative_to(ws.root))],
                ["-k", "not integration and not e2e and not performance and not live"],
            )
        js = await self.js_tests(ctx, ws)
        if js is not None:
            return js
        ext = ws.is_vscode_extension()
        if ext and (ext[0] / "node_modules").is_dir():
            sub = TargetWorkspace.detect(ext[0])
            js = await self.js_tests(ctx, sub, ext[0])
            if js is not None:
                return js
        return TestReport(self.key, skipped_reason="no unit tests or supported test runner found")


class IntegrationTestingAgent(TestingAgent):
    key = "integration-testing"
    name = "Integration Testing Agent"
    description = "Runs tests/integration (real databases/files/services) with pytest."

    async def execute(self, ctx: AgentContext, ws: TargetWorkspace, payload: dict[str, Any]) -> TestReport:
        d = (ws.tests_dir or ws.root / "tests") / "integration"
        if not d.is_dir():
            return TestReport(self.key, skipped_reason="no tests/integration directory")
        return await self.pytest(ctx, ws, [str(d.relative_to(ws.root))])


class EndToEndTestingAgent(TestingAgent):
    key = "e2e-testing"
    name = "End-to-End Testing Agent"
    description = "Runs tests/e2e (browser/extension flows). Reports `skipped` (never passed) when none exist or browsers are missing."
    default_timeout_s = 1200.0

    async def execute(self, ctx: AgentContext, ws: TargetWorkspace, payload: dict[str, Any]) -> TestReport:
        d = (ws.tests_dir or ws.root / "tests") / "e2e"
        if not d.is_dir():
            return TestReport(self.key, skipped_reason="no tests/e2e directory")
        return await self.pytest(ctx, ws, [str(d.relative_to(ws.root))])
