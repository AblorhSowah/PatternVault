"""Security, performance, regression and code-quality testing agents."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from app.agents.base import AgentContext
from app.extraction.language import CODE_LANGUAGES, PARSEABLE, detect_language
from app.extraction.structure import extract_structure
from app.models import RegressionBaseline, TestingSession
from app.models.base import utcnow
from app.security.exclusions import walk_project
from app.security.scanner import SEVERITY_ORDER, scan_text
from app.testing import parsers
from app.testing.agents.base import HARNESS, TestingAgent
from app.testing.agents.suites import IntegrationTestingAgent, UnitTestingAgent
from app.testing.report import TestCase, TestReport
from app.testing.workspace import TargetWorkspace

MAX_SCAN_FILES = 2000
FAIL_SEVERITY = {"high": 3, "critical": 4}


def _is_test_path(rel: str) -> bool:
    parts = rel.lower().replace("\\", "/").split("/")
    base = parts[-1]
    return (
        any(d in ("tests", "test", "__tests__", "spec", "e2e") for d in parts[:-1])
        or base.startswith("test_")
        or ".test." in base
        or ".spec." in base
        or base.endswith("_test.py")
    )


class SecurityTestingAgent(TestingAgent):
    key = "security-testing"
    name = "Security Testing Agent"
    description = "Static security rules, secret scan, bandit and dependency audits. Fails on high/critical findings; audits that cannot run are `skipped`."

    async def execute(self, ctx: AgentContext, ws: TargetWorkspace, payload: dict[str, Any]) -> TestReport:
        report = TestReport(self.key, command="static rules + secret scan + bandit + dependency audit")
        files = payload.get("files")
        paths = list(files) if files else walk_project(ws.root, ctx.svc.exclusion_policy())[:MAX_SCAN_FILES]
        if not payload.get("include_tests", False):  # test fixtures intentionally contain insecure code
            paths = [p for p in paths if not _is_test_path(p)]
        findings: list[dict[str, Any]] = []
        scanned = 0
        for rel in paths:
            lang = detect_language(rel)
            if lang not in CODE_LANGUAGES and not rel.endswith((".yml", ".yaml", ".json", ".toml", ".env.example")):
                continue
            try:
                text = (ws.root / rel).read_text(encoding="utf-8", errors="replace")[:400_000]
            except OSError:
                continue
            scanned += 1
            findings.extend(scan_text(rel, text))
        bad = [f for f in findings if SEVERITY_ORDER[f["severity"]] >= 3]
        report.cases.append(
            TestCase(
                f"static analysis of {scanned} file(s): no high/critical findings",
                "failed" if bad else "passed",
                expected="no high or critical findings",
                actual=f"{len(bad)} high/critical, {len(findings) - len(bad)} lower",
                error_details="\n".join(
                    f"{f['severity'].upper()} {f['rule']} {f['path']}:{f['line']} {f['title']} - {f['fix']}"
                    for f in bad[:40]
                ),
            )
        )
        for f in bad[:40]:
            report.cases.append(
                TestCase(
                    f"{f['rule']} {f['path']}:{f['line']}",
                    "failed",
                    description=f["title"],
                    expected="no finding",
                    actual=f["snippet"],
                    error_details=f["fix"],
                    file_path=f["path"],
                )
            )
        report.extra["findings"] = findings[:200]
        if ws.has_python_project and ws.python_has("bandit"):
            res = await self.sh(
                ctx,
                [ws.python, "-m", "bandit", "-q", "-r", ".", "-f", "json", "-x", ".venv,venv,node_modules,tests,test"],
                300,
            )
            results = parsers.parse_bandit_json(res.stdout)
            high = [r for r in results if r.get("issue_severity") in ("HIGH",)]
            report.cases.append(
                TestCase(
                    "bandit: no HIGH severity issues",
                    "failed" if high else "passed",
                    expected="0 high",
                    actual=f"{len(high)} high, {len(results) - len(high)} lower",
                    error_details="\n".join(
                        f"{r['filename']}:{r['line_number']} {r['test_id']} {r['issue_text']}" for r in high[:20]
                    ),
                )
            )
        else:
            report.cases.append(TestCase("bandit", "skipped", actual="bandit not installed or not a Python project"))
        if payload.get("dependency_audit", True):
            report.cases.append(await self._dep_audit(ctx, ws))
        else:
            report.cases.append(TestCase("dependency audit", "skipped", actual="disabled for this run"))
        return report

    async def _dep_audit(self, ctx: AgentContext, ws: TargetWorkspace) -> TestCase:
        if ws.node_ready() and (ws.root / "package-lock.json").exists():
            res = await self.sh(ctx, ["npm", "audit", "--json"], 120)
            try:
                data = json.loads(res.stdout)
                meta = data.get("metadata", {}).get("vulnerabilities", {})
                bad = int(meta.get("high", 0)) + int(meta.get("critical", 0))
                if "error" in data and not meta:
                    return TestCase("npm audit", "skipped", actual="audit could not run (offline?)")
                return TestCase(
                    "npm audit: no high/critical vulnerabilities",
                    "failed" if bad else "passed",
                    expected="0 high/critical",
                    actual=f"{bad} high/critical",
                    error_details=json.dumps(meta),
                )
            except ValueError:
                return TestCase("npm audit", "skipped", actual="audit produced no result (offline?)")
        req = ws.root / "requirements.txt"
        if ws.has_python_project and req.is_file() and ws.python_has("pip_audit"):
            res = await self.sh(
                ctx,
                [
                    ws.python,
                    "-m",
                    "pip_audit",
                    "-r",
                    "requirements.txt",
                    "--no-deps",
                    "--disable-pip",
                    "--format",
                    "json",
                    "--progress-spinner",
                    "off",
                ],
                180,
            )
            try:
                deps = json.loads(res.stdout).get("dependencies", [])
                vulns = [d for d in deps if d.get("vulns")]
                return TestCase(
                    "pip-audit: no known vulnerabilities",
                    "failed" if vulns else "passed",
                    expected="0 vulnerable packages",
                    actual=f"{len(vulns)} vulnerable of {len(deps)}",
                    error_details="\n".join(
                        f"{d['name']} {d['version']}: {[v['id'] for v in d['vulns']]}" for d in vulns[:20]
                    ),
                )
            except ValueError:
                return TestCase(
                    "pip-audit", "skipped", actual="audit could not run (requirements not pinned, or offline)"
                )
        return TestCase("dependency audit", "skipped", actual="no supported dependency audit tool/manifest available")


class PerformanceTestingAgent(TestingAgent):
    key = "performance-testing"
    name = "Performance Testing Agent"
    description = (
        "Micro-benchmarks against budgets (PatternVault projects) and suite-duration regression for any project."
    )

    BUDGETS = {"index_per_pattern_ms": 120.0, "search_p95_ms": 250.0, "parse_2000_lines_ms": 400.0}

    async def execute(self, ctx: AgentContext, ws: TargetWorkspace, payload: dict[str, Any]) -> TestReport:
        report = TestReport(self.key, command="perf probe")
        if ws.is_patternvault():
            budgets = {**self.BUDGETS, **(payload.get("budgets") or {})}
            res = await self.sh(
                ctx,
                [
                    ws.python,
                    str(HARNESS / "perf_probe.py"),
                    "--root",
                    str(ws.root),
                    "--patterns",
                    str(payload.get("patterns", 200)),
                ],
                900,
            )
            try:
                m = json.loads(res.stdout[res.stdout.index("{") :])
            except ValueError:
                report.error = (
                    f"perf probe produced no metrics (exit {res.returncode}): {(res.stderr or res.stdout)[-300:]}"
                )
                return report
            for name, limit in budgets.items():
                v = float(m[name])
                report.cases.append(
                    TestCase(
                        f"{name} ≤ {limit:g}",
                        "passed" if v <= limit else "failed",
                        expected=f"≤ {limit:g}",
                        actual=f"{v:.1f}",
                    )
                )
            report.extra["metrics"] = m
            return report
        # generic: unit-suite wall time within budget and no >50% slowdown vs the previous passing run
        sub = await UnitTestingAgent().execute(ctx, ws, {})
        if sub.executed == 0:
            report.skipped_reason = "no executable test suite to time"
            return report
        budget = float(payload.get("suite_budget_s", 600))
        report.cases.append(
            TestCase(
                "test suite duration within budget",
                "passed" if sub.duration_s <= budget else "failed",
                expected=f"≤ {budget:g}s",
                actual=f"{sub.duration_s:.1f}s",
            )
        )
        with ctx.svc.session_factory() as db:
            prev = (
                db.execute(
                    select(TestingSession)
                    .where(
                        TestingSession.project_id == ctx.project_id,
                        TestingSession.agent_key == self.key,
                        TestingSession.status == "passed",
                    )
                    .order_by(TestingSession.id.desc())
                )
                .scalars()
                .first()
            )
            prev_d = float((prev.summary or {}).get("suite_s", 0)) if prev else 0.0
        if prev_d:
            report.cases.append(
                TestCase(
                    "test suite not >50% slower than last passing run",
                    "passed" if sub.duration_s <= prev_d * 1.5 else "failed",
                    expected=f"≤ {prev_d * 1.5:.1f}s",
                    actual=f"{sub.duration_s:.1f}s",
                )
            )
        report.extra["suite_s"] = round(sub.duration_s, 2)
        return report


class RegressionTestingAgent(TestingAgent):
    key = "regression-testing"
    name = "Regression Testing Agent"
    description = "Compares current unit+integration results with the stored baseline: any previously-passing test that now fails is a regression."

    async def execute(self, ctx: AgentContext, ws: TargetWorkspace, payload: dict[str, Any]) -> TestReport:
        report = TestReport(self.key, command="unit + integration vs baseline")
        if ctx.project_id is None:
            report.skipped_reason = "regression testing needs a registered project"
            return report
        current: dict[str, str] = {}
        for agent in (UnitTestingAgent(), IntegrationTestingAgent()):
            r = ctx.results.get(agent.key)
            rep = r.output.get("report") if r else None
            cases = rep["cases"] if rep else [c.to_dict() for c in (await agent.execute(ctx, ws, {})).cases]
            for c in cases:
                if c["status"] in ("passed", "failed", "error"):
                    current[f"{agent.key}::{c['name']}"] = c["status"]
        if not current:
            report.skipped_reason = "no tests executed, so there is nothing to compare"
            return report
        with ctx.svc.session_factory() as db:
            base = {
                b.test_key: b.status
                for b in db.execute(
                    select(RegressionBaseline).where(RegressionBaseline.project_id == ctx.project_id)
                ).scalars()
            }
        regress = [k for k, st in base.items() if st == "passed" and current.get(k) in ("failed", "error")]
        missing = [k for k, st in base.items() if st == "passed" and k not in current]
        fixed = [k for k, st in base.items() if st != "passed" and current.get(k) == "passed"]
        for k in regress:
            report.cases.append(
                TestCase(
                    f"regression: {k}",
                    "failed",
                    expected="still passes (passed in baseline)",
                    actual=current[k],
                    file_path=None,
                )
            )
        if base:
            report.cases.append(
                TestCase(
                    f"no regressions across {len(base)} baseline test(s)",
                    "failed" if regress else "passed",
                    expected="0 regressions",
                    actual=f"{len(regress)} regression(s), {len(missing)} missing, {len(fixed)} newly fixed",
                )
            )
        else:
            report.notes.append("no baseline yet: this run establishes one if everything passes")
            report.skipped_reason = "no baseline to compare against"
        report.extra.update(
            {"regressions": regress, "missing": missing[:50], "newly_fixed": len(fixed), "tests_compared": len(current)}
        )
        if payload.get("update_baseline") and not regress and all(s == "passed" for s in current.values()):
            with ctx.svc.session_factory() as db:
                for k, st in current.items():
                    row = db.execute(
                        select(RegressionBaseline).where(
                            RegressionBaseline.project_id == ctx.project_id, RegressionBaseline.test_key == k
                        )
                    ).scalar_one_or_none()
                    if row is None:
                        db.add(RegressionBaseline(project_id=ctx.project_id, test_key=k, status=st))
                    else:
                        row.status, row.updated_at = st, utcnow()
                db.commit()
            report.notes.append(f"baseline updated with {len(current)} passing test(s)")
        return report


class CodeQualityAgent(TestingAgent):
    key = "code-quality"
    name = "Code Quality Agent"
    description = (
        "Lint (ruff/eslint), type checks (tsc) and structural complexity (very long functions, long parameter lists)."
    )

    LONG_FN = 150
    MANY_PARAMS = 8

    async def execute(self, ctx: AgentContext, ws: TargetWorkspace, payload: dict[str, Any]) -> TestReport:
        report = TestReport(self.key, command="ruff / eslint / tsc / complexity")
        ran = 0
        if ws.has_python_project and ws.python_has("ruff"):
            res = await self.sh(
                ctx, [ws.python, "-m", "ruff", "check", "--output-format", "json", "--no-cache", "."], 300
            )
            found = parsers.parse_ruff_json(res.stdout)
            ran += 1
            report.cases.extend(found or [TestCase("ruff: no lint errors", "passed", expected="clean", actual="clean")])
        ext = ws.is_vscode_extension()
        sub_ws = TargetWorkspace.detect(ext[0]) if ext else ws
        base = ext[0] if ext else ws.root
        if sub_ws.node_ready() and sub_ws.node_dep("typescript"):
            res = await self.sh(ctx, ["npx", "tsc", "--noEmit"], 600, cwd=base)
            ran += 1
            report.cases.extend(
                parsers.parse_tsc(res.stdout + res.stderr)
                or [
                    TestCase(
                        "tsc: no type errors",
                        "passed" if res.ok else "error",
                        expected="clean",
                        actual="clean" if res.ok else (res.stdout + res.stderr)[-300:],
                    )
                ]
            )
        if sub_ws.node_ready() and sub_ws.node_dep("eslint"):
            res = await self.sh(ctx, ["npx", "eslint", "-f", "json", "."], 600, cwd=base)
            ran += 1
            report.cases.extend(
                parsers.parse_eslint_json(res.stdout)
                or [TestCase("eslint: no errors", "passed", expected="clean", actual="clean")]
            )
        long_fns, wide = 0, 0
        for rel in walk_project(ws.root, ctx.svc.exclusion_policy())[:MAX_SCAN_FILES]:
            lang = detect_language(rel)
            if lang not in PARSEABLE:
                continue
            try:
                text = (ws.root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for s in extract_structure(lang, text).symbols:
                if s.kind in ("function", "method"):
                    if s.end_line - s.line + 1 > self.LONG_FN:
                        long_fns += 1
                        report.cases.append(
                            TestCase(
                                f"complexity: {rel}:{s.line} {s.name}",
                                "failed",
                                expected=f"≤ {self.LONG_FN} lines",
                                actual=f"{s.end_line - s.line + 1} lines",
                                file_path=rel,
                            )
                        )
                    if s.params > self.MANY_PARAMS:
                        wide += 1
                        report.notes.append(f"{rel}:{s.line} {s.name} has {s.params} parameters")
        ran += 1
        if not long_fns:
            report.cases.append(
                TestCase(
                    "complexity: no function longer than the limit",
                    "passed",
                    expected=f"≤ {self.LONG_FN} lines",
                    actual="ok",
                )
            )
        report.extra.update({"long_functions": long_fns, "wide_signatures": wide, "checks_run": ran})
        return report
