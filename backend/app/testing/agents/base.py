"""Shared base for the ten testing agents."""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any

from app.agents.base import Agent, AgentContext, AgentResult
from app.execution.commands import CommandDenied, CommandResult
from app.orchestration.approvals import ApprovalDenied, ApprovalTimeout
from app.testing import parsers
from app.testing.report import TestCase, TestReport, persist
from app.testing.workspace import TargetWorkspace

HARNESS = Path(__file__).resolve().parents[1] / "harness"


class TestingAgent(Agent):
    __test__ = False  # not a pytest class
    category = "testing"
    permissions = ("read_files", "run_verification_commands")
    default_timeout_s = 900.0

    async def execute(
        self, ctx: AgentContext, ws: TargetWorkspace, payload: dict[str, Any]
    ) -> TestReport:  # pragma: no cover - interface
        raise NotImplementedError

    async def run(self, ctx: AgentContext, payload: dict[str, Any]) -> AgentResult:
        root = ctx.require_root()
        ws = TargetWorkspace.detect(root)
        t0 = time.monotonic()
        report = TestReport(self.key)
        try:
            report = await self.execute(ctx, ws, payload)
        except (CommandDenied, ApprovalDenied, ApprovalTimeout) as exc:
            report = TestReport(self.key, error=f"command not permitted: {exc}")
        except Exception as exc:
            report = TestReport(self.key, error=f"{type(exc).__name__}: {exc}")
        report.duration_s = time.monotonic() - t0
        persist(ctx.svc, report, project_id=ctx.project_id, task_id=ctx.task_id)
        ctx.emit(
            "testing.report", {"agent": self.key, "status": report.status, "session_id": report.testing_session_id}
        )
        status = {"passed": "success", "skipped": "skipped"}.get(report.status, "failed")
        return AgentResult(
            status,
            f"{self.name}: {report.status} ({report.executed} executed, {len(report.failures)} failing)",
            {"report": report.to_dict(), "testing_session_id": report.testing_session_id},
            [{"level": "error", "message": f"{c.name}: {c.actual}"[:300]} for c in report.failures[:20]],
        )

    # ------------------------------------------------------------------ helpers
    async def sh(
        self, ctx: AgentContext, argv: list[str], timeout_s: float | None = None, cwd: Path | None = None
    ) -> CommandResult:
        assert ctx.policy is not None
        return await ctx.svc.commands.run(
            argv,
            cwd or ctx.require_root(),
            ctx.policy,
            timeout_s=timeout_s or self.default_timeout_s,
            task_id=ctx.task_id,
            actor=f"agent:{self.key}",
        )

    async def pytest(
        self, ctx: AgentContext, ws: TargetWorkspace, targets: list[str], extra: list[str] | None = None
    ) -> TestReport:
        report = TestReport(self.key)
        if not ws.python_has("pytest"):
            report.skipped_reason = f"pytest is not installed for {ws.python}"
            return report
        tmp = Path(tempfile.mkdtemp(prefix="pv-junit-")) / "result.xml"
        argv = [
            ws.python,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={tmp}",
            *(extra or []),
            *targets,
        ]
        report.command = " ".join(argv[:3]) + " " + " ".join(argv[3:])
        res = await self.sh(ctx, argv)
        if tmp.exists():
            report.cases = parsers.parse_junit(tmp.read_text(encoding="utf-8", errors="replace"))
            tmp.unlink(missing_ok=True)
        if res.timed_out:
            report.error = f"test run timed out after {self.default_timeout_s:.0f}s"
        elif not report.cases:
            tail = (res.stdout + res.stderr)[-600:]
            if res.returncode == 5:
                report.skipped_reason = "pytest collected no tests"
            else:
                report.error = f"pytest produced no results (exit {res.returncode}): {tail}"
        elif res.returncode not in (0, 1):
            report.notes.append(f"pytest exited with code {res.returncode}")
        return report

    async def js_tests(self, ctx: AgentContext, ws: TargetWorkspace, base: Path | None = None) -> TestReport | None:
        base = base or ws.root
        pkg = ws
        if not ws.node_ready():
            return None
        if pkg.node_dep("vitest"):
            res = await self.sh(ctx, ["npx", "vitest", "run", "--reporter=json"], cwd=base)
            report = TestReport(self.key, command="npx vitest run --reporter=json")
            report.cases = parsers.parse_vitest_json(res.stdout)
            if not report.cases:
                report.error = (
                    f"vitest produced no parsable results (exit {res.returncode}): {(res.stdout + res.stderr)[-400:]}"
                )
            return report
        if pkg.node_dep("jest"):
            res = await self.sh(ctx, ["npx", "jest", "--json"], cwd=base)
            report = TestReport(self.key, command="npx jest --json")
            report.cases = parsers.parse_vitest_json(res.stdout)
            if not report.cases:
                report.error = f"jest produced no parsable results (exit {res.returncode})"
            return report
        return None

    @staticmethod
    def cases_from_probe(stdout: str) -> tuple[list[TestCase], str]:
        try:
            data = json.loads(stdout[stdout.index("{") :])
        except (ValueError, json.JSONDecodeError):
            return [], "harness produced no JSON output"
        cases = [
            TestCase(
                c["name"],
                c["status"],
                expected=c.get("expected", ""),
                actual=c.get("actual", ""),
                error_details=c.get("details", ""),
            )
            for c in data.get("cases", [])
        ]
        return cases, data.get("error", "")
