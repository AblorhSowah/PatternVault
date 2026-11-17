"""Test plan: the ten dedicated testing agents run against REAL mini projects (and PatternVault itself)
Honesty rules: nothing executed ⇒ `skipped` (never passed); failures carry name/expected/actual/error details; a crash or
denied command ⇒ `error`; results are persisted (testing_sessions/test_results) and reach the orchestrator as AgentResult.
Per agent: pass case, fail case, skip case where applicable.
"""

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from app.agents.base import AgentContext
from app.config import PermissionLevel as L
from app.models import Project, TestingSession, TestResult
from app.security.permissions import PermissionPolicy
from app.testing.report import TestCase, TestReport
from tests.conftest import FAKE_AWS

pytestmark = pytest.mark.integration
REPO = Path(__file__).resolve().parents[2]


def mkproject(svc, tmp_path, files: dict[str, str], name="target"):
    root = tmp_path / name
    for rel, text in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    with svc.session_factory() as db:
        p = Project(name=name, root_path=str(root))
        db.add(p)
        db.commit()
        return root, p.id


def run_agent(svc, key, root, pid, level=L.RESTRICTED_AUTONOMOUS, payload=None, results=None):
    ctx = AgentContext(
        svc, None, pid, root, PermissionPolicy(level, root, plan_approved=True), "test", {}, results or {}
    )
    return asyncio.run(svc.agents.get(key).run(ctx, payload or {}))


def report_of(res):
    return res.output["report"]


PASSING = {
    "requirements.txt": "pytest\n",
    "tests/unit/test_ok.py": "def test_a():\n    assert 1 + 1 == 2\n\n\ndef test_b():\n    assert 'a'.upper() == 'A'\n",
}
FAILING = {
    "requirements.txt": "pytest\n",
    "tests/unit/test_bad.py": "def test_ok():\n    assert True\n\n\ndef test_broken():\n    x = {'k': 1}\n    assert x['k'] == 2, 'wrong value'\n",
}


# ------------------------------------------------------------------ report honesty rules
def test_report_status_rules():
    assert TestReport("a").status == "skipped"  # nothing executed
    assert TestReport("a", cases=[TestCase("x", "skipped")]).status == "skipped"
    assert TestReport("a", cases=[TestCase("x", "passed")]).status == "passed"
    assert TestReport("a", cases=[TestCase("x", "passed"), TestCase("y", "failed")]).status == "failed"
    assert TestReport("a", cases=[TestCase("x", "passed")], error="boom").status == "error"
    assert TestReport("a", cases=[TestCase("x", "passed")], skipped_reason="why").status == "skipped"
    assert TestReport("a", cases=[TestCase("x", "error")]).status == "failed"


# ------------------------------------------------------------------ unit / integration / e2e
def test_unit_agent_pass_fail_skip_and_persistence(services, tmp_path):
    root, pid = mkproject(services, tmp_path, PASSING, "ok")
    ok = run_agent(services, "unit-testing", root, pid)
    rep = report_of(ok)
    assert (
        ok.status == "success"
        and rep["status"] == "passed"
        and rep["summary"]["passed"] == 2
        and rep["summary"]["executed"] == 2
    )
    with services.session_factory() as db:
        ts = db.get(TestingSession, rep["testing_session_id"])
        assert (
            ts.status == "passed" and ts.agent_key == "unit-testing" and len(ts.results) == 2 and "pytest" in ts.command
        )
    root2, pid2 = mkproject(services, tmp_path, FAILING, "bad")
    bad = run_agent(services, "unit-testing", root2, pid2)
    case = next(c for c in report_of(bad)["cases"] if c["status"] == "failed")
    assert (
        bad.status == "failed"
        and "test_broken" in case["name"]
        and "wrong value" in case["error_details"]
        and case["expected"]
        and case["actual"]
    )
    assert bad.issues and "wrong value" in bad.issues[0]["message"]
    root3, pid3 = mkproject(
        services, tmp_path, {"requirements.txt": "x\n", "tests/unit/test_empty.py": "# nothing\n"}, "empty"
    )
    empty = run_agent(services, "unit-testing", root3, pid3)
    assert empty.status == "skipped" and report_of(empty)["summary"]["skipped_reason"] == "pytest collected no tests"
    root4, pid4 = mkproject(services, tmp_path, {"README.md": "x"}, "none")
    none = run_agent(services, "unit-testing", root4, pid4)
    assert none.status == "skipped" and "no unit tests" in report_of(none)["summary"]["skipped_reason"]


def test_integration_and_e2e_skip_without_dirs_and_run_with(services, tmp_path):
    root, pid = mkproject(
        services,
        tmp_path,
        {
            **PASSING,
            "tests/integration/test_i.py": "def test_i():\n    assert True\n",
            "tests/e2e/test_e.py": "def test_e():\n    assert True\n",
        },
        "full",
    )
    assert run_agent(services, "integration-testing", root, pid).status == "success"
    assert run_agent(services, "e2e-testing", root, pid).status == "success"
    root2, pid2 = mkproject(services, tmp_path, PASSING, "partial")
    assert run_agent(services, "integration-testing", root2, pid2).status == "skipped"
    assert run_agent(services, "e2e-testing", root2, pid2).status == "skipped"


def test_commands_go_through_the_permission_gate(services, tmp_path):
    root, pid = mkproject(services, tmp_path, PASSING, "ro")
    res = run_agent(services, "unit-testing", root, pid, level=L.READ_ONLY)
    assert (
        res.status == "failed"
        and report_of(res)["status"] == "error"
        and "not permitted" in report_of(res)["summary"]["error"]
    )
    # approvals mode: 'assisted' makes each command wait for a human; with nobody answering, it must not run
    from sqlalchemy import select

    from app.models import AuditLog

    with services.session_factory() as db:
        assert any(a.outcome == "denied" for a in db.scalars(select(AuditLog).where(AuditLog.action == "command.run")))


# ------------------------------------------------------------------ API
API_GOOD = """from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


class Item(BaseModel):
    name: str


@app.get("/ok")
def ok():
    return {"ok": 1}


@app.post("/items")
def create(item: Item):
    return {"name": item.name}
"""


def test_api_agent_good_and_broken_apps(services, tmp_path):
    root, pid = mkproject(services, tmp_path, {"app/__init__.py": "", "app/main.py": API_GOOD}, "api_ok")
    ok = run_agent(services, "api-testing", root, pid)
    names = [c["name"] for c in report_of(ok)["cases"]]
    assert ok.status == "success", report_of(ok)
    assert any("POST /items: validates required fields" in n for n in names) and any(
        "GET /ok: response matches" in n or "GET /ok: no server error" in n for n in names
    )
    broken = (
        API_GOOD
        + '\n\n@app.get("/boom")\ndef boom():\n    raise RuntimeError("kaboom")\n\n\n@app.post("/unvalidated")\ndef unvalidated(payload: dict):\n    return payload\n'
    )
    root2, pid2 = mkproject(services, tmp_path, {"app/__init__.py": "", "app/main.py": broken}, "api_bad")
    bad = run_agent(services, "api-testing", root2, pid2)
    failing = [c["name"] for c in report_of(bad)["cases"] if c["status"] == "failed"]
    assert bad.status == "failed" and any("GET /boom" in n for n in failing)
    root3, pid3 = mkproject(services, tmp_path, {"README.md": "no api"}, "api_none")
    assert run_agent(services, "api-testing", root3, pid3).status == "skipped"


def test_api_agent_on_patternvault_itself_checks_auth_enforcement():
    # PatternVault as a target: every route except /health must reject unauthenticated calls, and nothing may 5xx.
    import tempfile

    from app.config import RuntimeSettings
    from app.services.container import Services

    svc = Services(RuntimeSettings(home=Path(tempfile.mkdtemp())))
    try:
        with svc.session_factory() as db:
            p = Project(name="pv", root_path=str(REPO))
            db.add(p)
            db.commit()
            pid = p.id
        res = run_agent(svc, "api-testing", REPO, pid, level=L.RESTRICTED_AUTONOMOUS)
        rep = report_of(res)
        assert res.status == "success", [c for c in rep["cases"] if c["status"] != "passed"][:3]
        assert rep["summary"]["passed"] > 100 and any("rejects unauthenticated" in c["name"] for c in rep["cases"])
    finally:
        svc.close()


# ------------------------------------------------------------------ database
def test_database_agent_sqlite_integrity_and_fk(services, tmp_path):
    root, pid = mkproject(services, tmp_path, {"README.md": "x"}, "db_ok")
    con = sqlite3.connect(root / "app.db")
    con.executescript(
        "CREATE TABLE a(id INTEGER PRIMARY KEY); CREATE TABLE b(id INTEGER PRIMARY KEY, a_id INTEGER REFERENCES a(id)); INSERT INTO a VALUES (1); INSERT INTO b VALUES (1, 1);"
    )
    con.commit()
    con.close()
    ok = run_agent(services, "database-testing", root, pid)
    assert ok.status == "success" and {c["name"] for c in report_of(ok)["cases"]} == {
        "app.db: integrity_check",
        "app.db: foreign_key_check",
    }
    con = sqlite3.connect(root / "app.db")
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("INSERT INTO b VALUES (2, 999)")  # orphan row
    con.commit()
    con.close()
    bad = run_agent(services, "database-testing", root, pid)
    fk = next(c for c in report_of(bad)["cases"] if "foreign_key_check" in c["name"])
    assert bad.status == "failed" and fk["status"] == "failed" and "1 violation" in fk["actual"]
    root2, pid2 = mkproject(services, tmp_path, {"README.md": "x"}, "db_none")
    assert run_agent(services, "database-testing", root2, pid2).status == "skipped"


def test_database_agent_runs_the_real_migration_roundtrip_on_patternvault():
    import tempfile

    from app.config import RuntimeSettings
    from app.services.container import Services

    svc = Services(RuntimeSettings(home=Path(tempfile.mkdtemp())))
    try:
        with svc.session_factory() as db:
            p = Project(name="pv", root_path=str(REPO))
            db.add(p)
            db.commit()
            pid = p.id
        res = run_agent(svc, "database-testing", REPO, pid)
        names = [c["name"] for c in report_of(res)["cases"]]
        assert res.status == "success", [c for c in report_of(res)["cases"] if c["status"] != "passed"]
        assert (
            any("no drift" in n for n in names)
            and any("downgrade to base" in n for n in names)
            and any("re-upgrade" in n for n in names)
        )
    finally:
        svc.close()


# ------------------------------------------------------------------ extension
def ext_files(commands_in_src: list[str], declared: list[str]):
    return {
        "package.json": json.dumps(
            {
                "name": "x",
                "version": "1.0.0",
                "publisher": "p",
                "main": "./dist/extension.js",
                "engines": {"vscode": "^1.90.0"},
                "contributes": {"commands": [{"command": c, "title": c} for c in declared]},
            }
        ),
        "src/extension.ts": "\n".join(f"vscode.commands.registerCommand('{c}', () => 1);" for c in commands_in_src),
    }


def test_extension_agent_manifest_parity(services, tmp_path):
    root, pid = mkproject(services, tmp_path, ext_files(["x.one", "x.two"], ["x.one", "x.two"]), "ext_ok")
    ok = run_agent(services, "extension-testing", root, pid)
    assert ok.status == "success" and any("node_modules missing" in n for n in report_of(ok)["summary"]["notes"])
    root2, pid2 = mkproject(services, tmp_path, ext_files(["x.one"], ["x.one", "x.ghost"]), "ext_bad")
    bad = run_agent(services, "extension-testing", root2, pid2)
    assert bad.status == "failed" and any(
        "x.ghost" in c["name"] and c["status"] == "failed" for c in report_of(bad)["cases"]
    )
    root3, pid3 = mkproject(services, tmp_path, {"package.json": '{"name":"web"}'}, "not_ext")
    assert run_agent(services, "extension-testing", root3, pid3).status == "skipped"


# ------------------------------------------------------------------ security
def test_security_agent_flags_real_problems_and_passes_clean_code(services, tmp_path):
    bad_code = f'''import subprocess, hashlib, requests

API_KEY = "{FAKE_AWS}"


def run(user_input, cursor, name):
    eval(user_input)
    cursor.execute(f"SELECT * FROM users WHERE name = '{{name}}'")
    subprocess.run(user_input, shell=True)
    requests.get("https://x.example", verify=False)
    return hashlib.md5(name.encode()).hexdigest()
'''
    root, pid = mkproject(services, tmp_path, {"app/bad.py": bad_code, "requirements.txt": "requests\n"}, "sec_bad")
    res = run_agent(services, "security-testing", root, pid, payload={"dependency_audit": False})
    rules = {f["rule"] for f in report_of(res)["summary"]["findings"]}
    assert res.status == "failed" and {"PV-EVAL", "PV-SQLI-PY", "PV-SHELL-TRUE", "PV-TLS-VERIFY", "PV-SECRET"} <= rules
    assert any("PV-EVAL" in c["name"] and c["status"] == "failed" for c in report_of(res)["cases"])
    clean = "import os\n\n\ndef total(values):\n    return sum(v for v in values if v)\n\n\nKEY = os.environ.get('API_KEY')\n"
    root2, pid2 = mkproject(services, tmp_path, {"app/good.py": clean, "requirements.txt": "x\n"}, "sec_ok")
    ok = run_agent(services, "security-testing", root2, pid2, payload={"dependency_audit": False})
    assert ok.status == "success"
    skipped = [c for c in report_of(ok)["cases"] if c["status"] == "skipped"]
    assert skipped and "disabled" in skipped[0]["actual"]  # an audit that did not run is `skipped`, never `passed`


# ------------------------------------------------------------------ performance (PatternVault itself, real benchmarks)
def test_performance_agent_enforces_budgets():
    import tempfile

    from app.config import RuntimeSettings
    from app.services.container import Services

    svc = Services(RuntimeSettings(home=Path(tempfile.mkdtemp())))
    try:
        with svc.session_factory() as db:
            p = Project(name="pv", root_path=str(REPO))
            db.add(p)
            db.commit()
            pid = p.id
        ok = run_agent(svc, "performance-testing", REPO, pid, payload={"patterns": 40})
        m = report_of(ok)["summary"]["metrics"]
        assert (
            ok.status == "success" and m["patterns"] == 40 and m["search_p95_ms"] > 0 and m["index_per_pattern_ms"] > 0
        )
        tight = run_agent(
            svc, "performance-testing", REPO, pid, payload={"patterns": 40, "budgets": {"search_p95_ms": 0.001}}
        )
        assert tight.status == "failed" and any(
            "search_p95_ms" in c["name"] and c["status"] == "failed" for c in report_of(tight)["cases"]
        )
    finally:
        svc.close()


# ------------------------------------------------------------------ regression
def test_regression_agent_baseline_lifecycle(services, tmp_path):
    root, pid = mkproject(services, tmp_path, PASSING, "reg")
    first = run_agent(services, "regression-testing", root, pid, payload={"update_baseline": True})
    assert first.status == "skipped" and "baseline updated with 2" in " ".join(report_of(first)["summary"]["notes"])
    same = run_agent(services, "regression-testing", root, pid)
    assert same.status == "success" and report_of(same)["summary"]["tests_compared"] == 2
    (root / "tests/unit/test_ok.py").write_text(
        "def test_a():\n    assert 1 + 1 == 3\n\n\ndef test_b():\n    assert 'a'.upper() == 'A'\n"
    )
    broke = run_agent(services, "regression-testing", root, pid, payload={"update_baseline": True})
    assert broke.status == "failed" and report_of(broke)["summary"]["regressions"] == [
        "unit-testing::tests.unit.test_ok::test_a"
    ]
    assert "baseline updated" not in " ".join(
        report_of(broke)["summary"]["notes"]
    )  # a failing run never overwrites the baseline
    (root / "tests/unit/test_ok.py").write_text(
        PASSING["tests/unit/test_ok.py"] + "\n\ndef test_new():\n    assert True\n"
    )
    fixed = run_agent(services, "regression-testing", root, pid, payload={"update_baseline": True})
    assert fixed.status == "success" and report_of(fixed)["summary"]["tests_compared"] == 3


def test_regression_agent_requires_a_project_and_tests(services, tmp_path):
    root, pid = mkproject(services, tmp_path, {"README.md": "x"}, "reg_none")
    assert run_agent(services, "regression-testing", root, pid).status == "skipped"
    assert run_agent(services, "regression-testing", root, None).status == "skipped"


# ------------------------------------------------------------------ code quality
def test_quality_agent_lint_and_complexity(services, tmp_path):
    root, pid = mkproject(
        services,
        tmp_path,
        {"requirements.txt": "x\n", "pkg/mod.py": "import os\n\n\ndef f():\n    return 1\n"},
        "q_bad",
    )
    bad = run_agent(services, "code-quality", root, pid)
    assert bad.status == "failed" and any(
        c["name"].startswith("ruff:") and "F401" in c["error_details"] for c in report_of(bad)["cases"]
    )
    long_fn = "def big():\n" + "".join(f"    x{i} = {i}\n" for i in range(170)) + "    return x1\n"
    root2, pid2 = mkproject(services, tmp_path, {"requirements.txt": "x\n", "pkg/big.py": long_fn}, "q_long")
    res = run_agent(services, "code-quality", root2, pid2)
    assert res.status == "failed" and any(
        (c["name"].startswith("complexity:") and "170" in c["actual"]) or "172" in c["actual"]
        for c in report_of(res)["cases"]
        if c["status"] == "failed"
    )
    root3, pid3 = mkproject(
        services, tmp_path, {"requirements.txt": "x\n", "pkg/ok.py": "def f(a):\n    return a + 1\n"}, "q_ok"
    )
    assert run_agent(services, "code-quality", root3, pid3).status == "success"


# ------------------------------------------------------------------ registry / results visible to the UI
def test_all_ten_testing_agents_registered_and_persisted_results_queryable(services, tmp_path):
    keys = {a["key"] for a in services.agents.describe() if a["category"] == "testing"}
    assert keys == {
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
    }
    root, pid = mkproject(services, tmp_path, FAILING, "q")
    run_agent(services, "unit-testing", root, pid)
    with services.session_factory() as db:
        failed = db.query(TestResult).filter(TestResult.status == "failed").all()
        assert failed and failed[0].error_details and failed[0].expected
