"""API, database and extension testing agents (probe harnesses run in the target interpreter)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from app.agents.base import AgentContext
from app.security.exclusions import EXCLUDED_DIRS
from app.testing import parsers
from app.testing.agents.base import HARNESS, TestingAgent
from app.testing.report import TestCase, TestReport
from app.testing.workspace import TargetWorkspace


def detect_app_spec(ws: TargetWorkspace, payload: dict[str, Any]) -> str | None:
    if payload.get("app"):
        return str(payload["app"])
    if ws.is_patternvault():
        return "app.main:create_app"
    for rel, mod in (
        ("app/main.py", "app.main"),
        ("main.py", "main"),
        ("src/main.py", "src.main"),
        ("backend/app/main.py", "app.main"),
    ):
        f = ws.root / rel
        if f.is_file():
            text = f.read_text(encoding="utf-8", errors="ignore")
            if m := re.search(r"^(\w+)\s*=\s*FastAPI\(", text, re.M):
                return f"{mod}:{m.group(1)}"
            if "def create_app" in text:
                return f"{mod}:create_app"
    return None


def find_db_files(root: Path, limit: int = 10) -> list[Path]:
    """SQLite files in the project, pruning virtualenvs, VCS data and tool caches (mypy/ruff/pytest write *.db caches)."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix in (".db", ".sqlite", ".sqlite3") and not p.is_symlink() and p.stat().st_size > 0:
                found.append(p)
    return sorted(found)[:limit]


class ApiTestingAgent(TestingAgent):
    key = "api-testing"
    name = "API Testing Agent"
    description = "Probes a FastAPI app via its OpenAPI schema: no 5xx on bare requests, required-field validation, auth enforcement, response shape."

    async def execute(self, ctx: AgentContext, ws: TargetWorkspace, payload: dict[str, Any]) -> TestReport:
        spec = detect_app_spec(ws, payload)
        if not spec:
            return TestReport(
                self.key, skipped_reason="no FastAPI application detected (pass `app: module:attr` to test one)"
            )
        if not ws.python_has("fastapi"):
            return TestReport(self.key, skipped_reason=f"fastapi is not installed for {ws.python}")
        token = "probe-token-" + "x" * 24
        argv = [ws.python, str(HARNESS / "api_probe.py"), "--root", str(ws.root), "--app", spec]
        if ws.is_patternvault():
            argv += ["--expect-auth", "--token", token]
        report = TestReport(self.key, command=" ".join(argv[1:]))
        res = (
            await self.sh(ctx, argv, 300, cwd=ws.root)
            if not ws.is_patternvault()
            else await self._pv(ctx, ws, argv, token)
        )
        report.cases, err = self.cases_from_probe(res.stdout)
        if err or not report.cases:
            report.error = err or f"probe failed (exit {res.returncode}): {(res.stderr or res.stdout)[-400:]}"
        return report

    async def _pv(self, ctx: AgentContext, ws: TargetWorkspace, argv: list[str], token: str) -> Any:
        # The probe app must accept our token: PatternVault reads PATTERNVAULT_TOKEN. (sanitized_env drops secrets-like
        # vars, so the token is passed on the command line to the harness which sets it itself.)
        argv = [*argv, "--public", "/api/v1/health"]
        return await ctx.svc.commands.run(
            argv,
            ws.root,
            ctx.policy,
            timeout_s=300,
            task_id=ctx.task_id,
            actor=f"agent:{self.key}",
            env={"PATTERNVAULT_TOKEN": token},
        )


class DatabaseTestingAgent(TestingAgent):
    key = "database-testing"
    name = "Database Testing Agent"
    description = "SQLite integrity + foreign-key checks on database files; migration round trip and ORM-drift check for PatternVault-style projects."

    async def execute(self, ctx: AgentContext, ws: TargetWorkspace, payload: dict[str, Any]) -> TestReport:
        dbs = find_db_files(ws.root)
        argv = [ws.python, str(HARNESS / "db_probe.py"), "--root", str(ws.root)]
        for p in dbs:
            argv += ["--sqlite", str(p)]
        if ws.is_patternvault():
            argv.append("--patternvault")
        if not dbs and not ws.is_patternvault():
            return TestReport(
                self.key, skipped_reason="no SQLite database files and no supported migration setup found"
            )
        report = TestReport(self.key, command=" ".join(argv[1:]))
        res = await self.sh(ctx, argv, 300)
        report.cases, err = self.cases_from_probe(res.stdout)
        if err or not report.cases:
            report.error = err or f"probe failed (exit {res.returncode}): {(res.stderr or res.stdout)[-400:]}"
        return report


class ExtensionTestingAgent(TestingAgent):
    key = "extension-testing"
    name = "Extension Testing Agent"
    description = (
        "VS Code extension checks: manifest sanity, declared-vs-registered command parity, `tsc --noEmit`, unit tests."
    )

    async def execute(self, ctx: AgentContext, ws: TargetWorkspace, payload: dict[str, Any]) -> TestReport:
        found = ws.is_vscode_extension()
        if not found:
            return TestReport(self.key, skipped_reason="not a VS Code extension project (no engines.vscode)")
        base, manifest = found
        report = TestReport(self.key, command="manifest checks + tsc --noEmit + unit tests")
        cases = report.cases
        for field_name in ("name", "version", "publisher", "main"):
            cases.append(
                TestCase(
                    f"manifest has '{field_name}'",
                    "passed" if manifest.get(field_name) else "failed",
                    expected="field present",
                    actual=str(manifest.get(field_name))[:80],
                )
            )
        declared = {c["command"] for c in (manifest.get("contributes", {}).get("commands") or [])}
        src = (
            "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in (base / "src").rglob("*.ts"))
            if (base / "src").is_dir()
            else ""
        )
        registered = set(re.findall(r"""['"]([\w.-]+\.[\w.-]+)['"]""", src)) & set(declared)
        for cmd in sorted(declared):
            cases.append(
                TestCase(
                    f"command '{cmd}' is declared and referenced in source",
                    "passed" if cmd in registered else "failed",
                    expected="command id appears in extension source",
                    actual="found" if cmd in registered else "never referenced",
                )
            )
        main = manifest.get("main")
        if main and (base / "dist").is_dir():
            cases.append(
                TestCase(
                    "entry point exists after build",
                    "passed" if (base / main).is_file() else "failed",
                    expected=str(main),
                    actual="present" if (base / main).is_file() else "missing",
                )
            )
        sub = TargetWorkspace.detect(base)
        if sub.node_ready():
            tsc = await self.sh(ctx, ["npx", "tsc", "--noEmit"], 600, cwd=base)
            errs = parsers.parse_tsc(tsc.stdout + tsc.stderr)
            cases.extend(
                errs
                or [
                    TestCase(
                        "tsc --noEmit",
                        "passed" if tsc.ok else "error",
                        expected="no type errors",
                        actual="clean" if tsc.ok else (tsc.stdout + tsc.stderr)[-300:],
                    )
                ]
            )
            js = await self.js_tests(ctx, sub, base)
            if js is not None:
                cases.extend(js.cases)
                if js.error:
                    report.error = js.error
        else:
            report.notes.append("node_modules missing: skipped tsc and unit tests")
        return report


def read_json(path: Path) -> dict[str, Any]:
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}
