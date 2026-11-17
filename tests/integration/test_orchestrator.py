"""Test plan: the Orchestrator end-to-end with REAL files, REAL pytest runs and a scripted model (Ollama protocol double).
Happy path (new + adapt + replay) · approvals (plan / per-write) · review blocking · correction loop (LLM fix, learned solution reuse)
· fix exhaustion → automatic rollback · read-only · verification-gate honesty (no tests ⇒ never verified) · locks/ordering ·
cancellation · disabled agents · agent crash · history/report API · knowledge learning rules (verified only via passing tests).
"""

import asyncio
import contextlib
import json
from pathlib import Path

import pytest

from app.extraction.extractor import FileInput, SessionInput, extract_candidates
from app.models import AgentExecution, DebuggingSolution, TestResult
from app.orchestration.orchestrator import TaskRequest
from tests.helpers.fake_ollama import FakeOllama

pytestmark = pytest.mark.integration

CALC = "def add(a, b):\n    return a + b\n"
CALC_TEST = "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
GOOD_CALC = CALC + "\n\ndef multiply(a, b):\n    return a * b\n"
BUGGY_CALC = CALC + "\n\ndef multiply(a, b):\n    return a + b\n"
MUL_TEST = "from calc import multiply\n\n\ndef test_multiply():\n    assert multiply(3, 4) == 12\n"


def blocks(calc: str, test: str = MUL_TEST) -> str:
    return (
        f'<<<FILE path="calc.py" action="modify">>>\n{calc}<<<END FILE>>>\n<<<FILE path="tests/unit/test_multiply.py" action="create">>>\n{test}<<<END FILE>>>\n'
        "<<<EXPLANATION>>>\nAdded multiply.\n<<<END EXPLANATION>>>"
    )


def mkcalc(svc, tmp_path, tests=True, name="calc-app"):
    from app.models import Project

    root = tmp_path / name
    (root / "tests/unit").mkdir(parents=True)
    (root / "requirements.txt").write_text("pytest\n")
    (root / "calc.py").write_text(CALC)
    if tests:
        (root / "tests/unit/test_calc.py").write_text(CALC_TEST)
        (root / "conftest.py").write_text(
            "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\n"
        )
    with svc.session_factory() as db:
        p = Project(
            name=name,
            root_path=str(root),
            conventions={"primary_language": "python", "analyzed_at": "x", "test_locations": ["tests"]},
        )
        db.add(p)
        db.commit()
        return root, p.id


def configure(svc, fake, level="restricted_autonomous", mode="offline", **agents):
    svc.config.update(
        {
            "mode": mode,
            "permission_level": level,
            "local_model": {"base_url": fake.url, "model": "qwen2.5-coder:7b"},
            "agents": agents or {},
        }
    )


def run_task(svc, kind, request, pid, params=None, approve=None, timeout=240):
    """Run a task to completion; `approve` decides every approval that appears (True/False) or None to leave it pending."""

    async def go():
        tid = await svc.orchestrator.submit(
            TaskRequest(kind, request, pid, {"agents": ["unit-testing"], **(params or {})})
        )
        if approve is not None:

            async def decider():
                while True:
                    for a in svc.approvals.pending():
                        svc.approvals.decide(a["id"], approve, "auto")
                    await asyncio.sleep(0.05)

            d = asyncio.create_task(decider())
            try:
                return await svc.orchestrator.wait(tid, timeout)
            finally:
                d.cancel()
        return await svc.orchestrator.wait(tid, timeout)

    return asyncio.run(go())


def steps(t):
    return {k: v["status"] for k, v in t["result"]["steps"].items()}


# ------------------------------------------------------------------ happy paths
def test_generate_task_verified_and_learned(services, tmp_path):
    root, pid = mkcalc(services, tmp_path)
    with FakeOllama(responder=lambda *a: blocks(GOOD_CALC)) as fake:
        configure(services, fake)
        t = run_task(services, "generate", "Add a multiply function to calc.py with a unit test", pid)
    assert t["status"] == "verified" and t["verified"] is True, t
    assert steps(t) == {
        "context": "success",
        "generate": "success",
        "review": "success",
        "apply": "success",
        "verify": "success",
        "learn": "success",
    }
    assert "def multiply" in (root / "calc.py").read_text() and (root / "tests/unit/test_multiply.py").exists()
    gate = t["result"]["gate"]
    assert gate["verified"] and gate["passed"] == ["unit-testing"] and gate["evidence_session_id"]
    assert t["result"]["recovery_point_id"] and sorted(t["result"]["files_applied"]) == [
        "calc.py",
        "tests/unit/test_multiply.py",
    ]
    # knowledge: a NEW pattern learned, `tested` through the passing session (never `verified` on its own)
    pats = services.patterns.list_patterns()["items"]
    assert len(pats) == 1 and pats[0]["status"] == "tested" and pats[0]["provenance"].get("session_id") is None
    # history: every agent execution recorded, in order
    kinds = [(e["agent"], e["step"]) for e in t["executions"]]
    assert kinds[:3] == [
        ("context-retrieval", "context"),
        ("code-generation", "generate"),
        ("code-review-security", "review"),
    ]
    assert ("unit-testing", "test:unit-testing") in kinds and ("pattern-extraction", "learn:extract") in kinds
    md = services.orchestrator.report_markdown(t["id"])
    assert "Verified:** yes" in md and "calc.py" in md and "unit-testing" in md


def test_adapt_task_uses_reference_pattern_and_records_success(services, tmp_path):
    root, pid = mkcalc(services, tmp_path)
    ref = extract_candidates(
        SessionInput(
            prompts=["Add a calculator multiply feature"],
            files=[FileInput("calc.py", "created", GOOD_CALC)],
            project_language="python",
        )
    )[0]
    pat = services.patterns.add_candidate(ref, project_id=pid, session_id=None).pattern_id
    prompts = []

    def responder(m, msgs, o):
        prompts.append(msgs[1]["content"])
        return blocks(GOOD_CALC)

    with FakeOllama(responder=responder) as fake:
        configure(services, fake)
        t = run_task(
            services,
            "generate",
            "Use the calculator multiply we built earlier but add it to calc.py",
            pid,
            {"pattern_ids": [pat]},
        )
    assert t["status"] == "verified"
    assert "multiply" in prompts[0] and "<<<UNTRUSTED_DATA" in prompts[0]
    p = services.patterns.get(pat, False)
    assert (
        p["use_count"] == 1 and p["success_count"] == 1 and p["status"] == "observed"
    )  # adaptation success counted, no free promotion


def test_replay_task_promotes_pattern_through_passing_tests(services, tmp_path):
    root, pid = mkcalc(services, tmp_path, tests=False, name="empty-calc")
    (root / "calc.py").unlink()
    cand = extract_candidates(
        SessionInput(
            prompts=["Add calculator"],
            files=[
                FileInput("calc.py", "created", GOOD_CALC),
                FileInput(
                    "tests/unit/test_calc.py",
                    "created",
                    "from calc import add, multiply\n\n\ndef test_calc():\n    assert add(1, 2) == 3\n    assert multiply(3, 4) == 12\n",
                ),
            ],
            project_language="python",
        )
    )[0]
    pat = services.patterns.add_candidate(cand, project_id=None, session_id=None).pattern_id
    (root / "conftest.py").write_text("import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\n")
    with FakeOllama() as fake:
        configure(services, fake)
        t = run_task(services, "replay", "Recreate the calculator implementation", pid, {"pattern_id": pat})
        assert fake.chats() == []  # replay needs no model
    assert t["status"] == "verified", t
    assert (root / "calc.py").read_text() == GOOD_CALC
    p = services.patterns.get(pat, False)
    assert (
        p["status"] == "verified" and p["success_count"] == 1 and p["confidence"] >= 0.8
    )  # tested via real test run + successful reuse


# ------------------------------------------------------------------ approvals
def test_approved_execution_waits_for_plan_approval(services, tmp_path):
    root, pid = mkcalc(services, tmp_path)

    async def go(approve):
        with FakeOllama(responder=lambda *a: blocks(GOOD_CALC)) as fake:
            configure(services, fake, level="approved_execution")
            tid = await services.orchestrator.submit(
                TaskRequest("generate", "Add multiply to calc.py", pid, {"agents": ["unit-testing"]})
            )
            for _ in range(200):
                await asyncio.sleep(0.05)
                if services.approvals.pending():
                    break
            pend = services.approvals.pending()
            assert (
                services.orchestrator.get(tid)["status"] == "awaiting_approval" and pend[0]["action"] == "approve_plan"
            )
            assert "multiply" not in (root / "calc.py").read_text()
            services.approvals.decide(pend[0]["id"], approve)
            return await services.orchestrator.wait(tid, 120)

    t = asyncio.run(go(True))
    assert t["status"] == "verified"
    root2, pid2 = mkcalc(services, tmp_path, name="calc-denied")
    pid = pid2
    root = root2
    t2 = asyncio.run(go(False))
    assert (
        t2["status"] == "failed" and "not approved" in t2["error"] and "multiply" not in (root2 / "calc.py").read_text()
    )


def test_assisted_mode_asks_for_every_write_and_command(services, tmp_path):
    root, pid = mkcalc(services, tmp_path)
    with FakeOllama(responder=lambda *a: blocks(GOOD_CALC)) as fake:
        configure(services, fake, level="assisted")
        t = run_task(services, "generate", "Add multiply to calc.py", pid, approve=True)
    assert t["status"] == "verified"
    actions = [a["action"] for a in services.approvals.list()]
    assert actions.count("write_file") == 2 and actions.count("run_command") >= 1
    assert all(a["status"] == "approved" for a in services.approvals.list())


def test_denied_write_approval_leaves_project_untouched(services, tmp_path):
    root, pid = mkcalc(services, tmp_path)
    with FakeOllama(responder=lambda *a: blocks(GOOD_CALC)) as fake:
        configure(services, fake, level="assisted")
        t = run_task(services, "generate", "Add multiply to calc.py", pid, approve=False)
    assert (
        t["status"] == "failed"
        and (root / "calc.py").read_text() == CALC
        and not (root / "tests/unit/test_multiply.py").exists()
    )


# ------------------------------------------------------------------ review & safety
def test_review_blocks_insecure_generation_unless_explicitly_overridden(services, tmp_path):
    root, pid = mkcalc(services, tmp_path)
    evil = CALC + '\n\ndef multiply(a, b):\n    return eval(f"{a}*{b}")\n'
    with FakeOllama(responder=lambda *a: blocks(evil)) as fake:
        configure(services, fake)
        t = run_task(services, "generate", "Add multiply to calc.py", pid)
        assert t["status"] == "failed" and steps(t)["review"] == "failed" and steps(t)["apply"] == "skipped"
        assert (root / "calc.py").read_text() == CALC  # nothing was written
        t2 = run_task(services, "generate", "Add multiply to calc.py", pid, {"override_review": True})
        assert (
            steps(t2)["apply"] == "success" and "eval(" in (root / "calc.py").read_text()
        )  # explicit, audited override
    from sqlalchemy import select

    from app.models import AuditLog

    with services.session_factory() as db:
        assert db.scalars(select(AuditLog).where(AuditLog.action == "task.submit")).all()


def test_traversal_and_protected_paths_never_reach_disk(services, tmp_path):
    root, pid = mkcalc(services, tmp_path)
    out = (
        '<<<FILE path="../escape.py" action="create">>>\nx = 1\n<<<END FILE>>>\n<<<FILE path=".git/config" action="create">>>\nx\n<<<END FILE>>>\n'
        + blocks(GOOD_CALC)
    )
    with FakeOllama(responder=lambda *a: out) as fake:
        configure(services, fake)
        t = run_task(services, "generate", "Add multiply to calc.py", pid)
    assert (
        t["status"] == "verified"
        and not (tmp_path / "escape.py").exists()
        and sorted(t["result"]["files_applied"]) == ["calc.py", "tests/unit/test_multiply.py"]
    )


def test_read_only_mode_proposes_but_never_writes_or_runs(services, tmp_path):
    root, pid = mkcalc(services, tmp_path)
    with FakeOllama(responder=lambda *a: blocks(GOOD_CALC)) as fake:
        configure(services, fake, level="read_only")
        t = run_task(services, "generate", "Add multiply to calc.py", pid)
    assert t["status"] == "completed" and t["verified"] is False
    assert steps(t)["apply"] == "skipped" and steps(t)["verify"] == "skipped" and (root / "calc.py").read_text() == CALC
    assert t["result"]["proposal"] and t["result"]["gate"]["skipped"] == ["unit-testing"]


# ------------------------------------------------------------------ correction loop
def test_failing_tests_trigger_llm_fix_then_independent_retest_then_learning(services, tmp_path):
    root, pid = mkcalc(services, tmp_path)
    calls = {"gen": 0, "debug": 0}

    def responder(m, msgs, o):
        if "A test failed" in msgs[1]["content"]:
            calls["debug"] += 1
            return (
                blocks(GOOD_CALC, MUL_TEST).split('<<<FILE path="tests')[0]
                + "<<<EXPLANATION>>>\nmultiply added instead of multiplied\n<<<END EXPLANATION>>>"
            )
        calls["gen"] += 1
        return blocks(BUGGY_CALC)

    with FakeOllama(responder=responder) as fake:
        configure(services, fake)
        t = run_task(services, "generate", "Add a multiply function to calc.py with a unit test", pid)
    assert t["status"] == "verified" and t["result"]["fix_attempts"] == 1 and calls == {"gen": 1, "debug": 1}
    assert (root / "calc.py").read_text() == GOOD_CALC and t["result"]["rolled_back"] is False
    # the ORIGINAL failure records which correction was applied and the outcome of the independent retest
    with services.session_factory() as db:
        failed = db.query(TestResult).filter(TestResult.status == "failed").all()
        assert (
            failed
            and failed[0].correction_applied.startswith("llm:")
            and failed[0].retest_status == "passed"
            and "assert" in failed[0].error_details
        )
        sols = db.query(DebuggingSolution).all()
        assert (
            len(sols) == 1
            and sols[0].verified
            and sols[0].verified_by_testing_session_id
            and sols[0].fix["files"][0]["path"] == "calc.py"
        )
    execs = [(e["step"], e["status"]) for e in t["executions"]]
    assert ("fix-1:debugging", "success") in execs and ("retest-1:unit-testing", "success") in execs
    assert len(services.recovery.list(pid)) == 2  # one before the task, one before the fix attempt


def test_known_verified_solution_is_reused_without_the_model(services, tmp_path):
    root, pid = mkcalc(services, tmp_path)

    def r1(m, msgs, o):
        return (
            blocks(GOOD_CALC).split('<<<FILE path="tests')[0] + "<<<EXPLANATION>>>\nx\n<<<END EXPLANATION>>>"
            if "A test failed" in msgs[1]["content"]
            else blocks(BUGGY_CALC)
        )

    with FakeOllama(responder=r1) as fake:
        configure(services, fake)
        assert run_task(services, "generate", "Add a multiply function to calc.py", pid)["status"] == "verified"
    # second project, same bug → the stored, verified solution fixes it with NO debug model call
    root2, pid2 = mkcalc(services, tmp_path, name="calc-two")
    seen = []

    def r2(m, msgs, o):
        seen.append("debug" if "A test failed" in msgs[1]["content"] else "gen")
        return blocks(BUGGY_CALC)

    with FakeOllama(responder=r2) as fake:
        configure(services, fake)
        t = run_task(services, "generate", "Add a multiply function to calc.py", pid2)
    assert t["status"] == "verified" and seen == ["gen"], seen  # the fix came from the knowledge base
    assert "return a * b" in (root2 / "calc.py").read_text()
    dbg = next(e for e in t["executions"] if e["agent"] == "debugging")
    assert "previously verified solution" in dbg["summary"]


def test_exhausted_fix_attempts_roll_back_the_project(services, tmp_path):
    root, pid = mkcalc(services, tmp_path)
    ref = extract_candidates(
        SessionInput(
            prompts=["calculator multiply"],
            files=[FileInput("calc.py", "created", GOOD_CALC)],
            project_language="python",
        )
    )[0]
    pat = services.patterns.add_candidate(ref, project_id=pid, session_id=None).pattern_id
    with FakeOllama(responder=lambda *a: blocks(BUGGY_CALC)) as fake:  # the model never gets it right
        configure(services, fake, max_fix_attempts=2)
        t = run_task(services, "generate", "Add a multiply function to calc.py", pid, {"pattern_ids": [pat]})
    assert (
        t["status"] == "failed"
        and t["verified"] is False
        and t["result"]["rolled_back"] is True
        and t["result"]["fix_attempts"] == 2
    )
    assert (root / "calc.py").read_text() == CALC and not (
        root / "tests/unit/test_multiply.py"
    ).exists()  # exact original state restored
    p = services.patterns.get(pat, False)
    assert p["failure_count"] == 1 and p["status"] == "observed"
    assert all(
        x["status"] != "tested" for x in services.patterns.list_patterns()["items"]
    )  # nothing unverified was promoted


# ------------------------------------------------------------------ gate honesty
def test_project_without_tests_is_never_verified(services, tmp_path):
    root, pid = mkcalc(services, tmp_path, tests=False, name="no-tests")
    (root / "tests").rmdir() if not any((root / "tests").rglob("*.py")) and False else None
    import shutil

    shutil.rmtree(root / "tests")
    with FakeOllama(
        responder=lambda *a: '<<<FILE path="calc.py" action="modify">>>\n' + GOOD_CALC + "<<<END FILE>>>"
    ) as fake:
        configure(services, fake)
        t = run_task(services, "generate", "Add a multiply function to calc.py", pid)
    assert t["verified"] is False and t["status"] != "verified"
    assert t["result"]["gate"]["passed"] == [] and t["result"]["gate"]["skipped"] == ["unit-testing"]
    assert services.patterns.list_patterns()["total"] == 0  # "generation completed" is not a reason to learn anything


def test_gate_logic_unit():
    from app.agents.base import AgentResult
    from app.orchestration.orchestrator import Orchestrator

    ev = Orchestrator.evaluate_gate
    ok = AgentResult("success", output={"testing_session_id": 7})
    assert ev({"unit-testing": ok})["verified"] and ev({"unit-testing": ok})["evidence_session_id"] == 7
    assert not ev({"unit-testing": AgentResult("skipped")})["verified"]
    assert not ev({})["verified"]
    assert not ev({"unit-testing": ok, "integration-testing": AgentResult("failed")})["verified"]
    assert not ev({"unit-testing": ok, "security-testing": AgentResult("failed")})["verified"]
    assert ev({"unit-testing": ok, "e2e-testing": AgentResult("skipped")})[
        "verified"
    ]  # skipped agents neither help nor block
    assert ev({"code-quality": ok})["evidence_session_id"] is None  # lint passing is not test evidence for promotion


# ------------------------------------------------------------------ concurrency & lifecycle
def test_path_locks_serialise_writers_and_project_lock_serialises_tasks():
    from app.orchestration.locks import FileLockManager

    async def go():
        lm = FileLockManager()
        order = []

        async def writer(name, paths, delay):
            async with lm.paths(1, paths, name):
                order.append(f"{name}:start")
                await asyncio.sleep(delay)
                order.append(f"{name}:end")

        await asyncio.gather(
            writer("a", ["x.py", "y.py"], 0.15), writer("b", ["y.py", "z.py"], 0.01), writer("c", ["q.py"], 0.01)
        )
        # a and b both want y.py → never interleaved; c is independent and may run in parallel
        ia, ib = order.index("a:start"), order.index("b:start")
        first, second = ("a", "b") if ia < ib else ("b", "a")
        assert order.index(f"{first}:end") < order.index(f"{second}:start")
        async with lm.project(1):
            assert lm.is_locked(1, "x.py") is False
        # sorted acquisition: opposite orders can't deadlock
        await asyncio.wait_for(asyncio.gather(writer("d", ["m", "n"], 0.01), writer("e", ["n", "m"], 0.01)), 5)

    asyncio.run(go())


def test_two_tasks_on_one_project_run_their_mutations_one_after_the_other(services, tmp_path):
    root, pid = mkcalc(services, tmp_path)
    n = {"i": 0}

    def responder(m, msgs, o):
        n["i"] += 1
        mod = "calc_a" if n["i"] == 1 else "calc_b"
        return (
            f'<<<FILE path="{mod}.py" action="create">>>\ndef f():\n    return {n["i"]}\n<<<END FILE>>>\n'
            f'<<<FILE path="tests/unit/test_{mod}.py" action="create">>>\nfrom {mod} import f\n\n\ndef test_f():\n    assert f() == {n["i"]}\n<<<END FILE>>>'
        )

    with FakeOllama(responder=responder) as fake:
        configure(services, fake)

        async def go():
            t1 = await services.orchestrator.submit(
                TaskRequest("generate", "Add module a to the project", pid, {"agents": ["unit-testing"]})
            )
            t2 = await services.orchestrator.submit(
                TaskRequest("generate", "Add module b to the project", pid, {"agents": ["unit-testing"]})
            )
            return await services.orchestrator.wait(t1, 240), await services.orchestrator.wait(t2, 240)

        a, b = asyncio.run(go())
    assert a["status"] == "verified" and b["status"] == "verified"
    assert (root / "calc_a.py").exists() and (root / "calc_b.py").exists()
    spans = []
    for t in (a, b):
        mut = [e["id"] for e in t["executions"] if e["step"] == "apply" or e["step"].startswith(("test:", "learn"))]
        spans.append((min(mut), max(mut)))
    (a0, a1), (b0, b1) = sorted(spans)
    assert a1 < b0, spans  # the apply→test→learn cycles of the two tasks never interleaved


def test_cancel_stops_a_running_task_and_its_subprocess(services, tmp_path):
    root, pid = mkcalc(services, tmp_path)
    (root / "tests/unit/test_slow.py").write_text("import time\n\n\ndef test_slow():\n    time.sleep(60)\n")

    async def go():
        tid = await services.orchestrator.submit(TaskRequest("test", "Run tests", pid, {"agents": ["unit-testing"]}))
        for _ in range(100):
            await asyncio.sleep(0.1)
            if any(e["status"] == "running" for e in services.orchestrator.get(tid)["executions"]):
                break
        assert services.orchestrator.cancel(tid)
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await services.orchestrator.wait(tid, 20)
        await asyncio.sleep(0.5)
        return services.orchestrator.get(tid)

    import time

    t0 = time.monotonic()
    t = asyncio.run(go())
    assert t["status"] == "cancelled" and time.monotonic() - t0 < 30


def test_disabled_agent_is_skipped_and_agent_crash_is_reported(services, tmp_path, monkeypatch):
    root, pid = mkcalc(services, tmp_path)
    with FakeOllama(responder=lambda *a: blocks(GOOD_CALC)) as fake:
        configure(services, fake, disabled=["code-review-security"])
        t = run_task(services, "generate", "Add multiply to calc.py", pid)
        assert steps(t)["review"] == "skipped" and t["status"] == "verified"
        configure(services, fake)

        async def boom(self, ctx, payload):
            raise RuntimeError("agent exploded")

        monkeypatch.setattr(type(services.agents.get("code-generation")), "run", boom)
        t2 = run_task(services, "generate", "Add multiply to calc.py", pid)
    assert t2["status"] == "failed" and "agent exploded" in t2["error"] and steps(t2)["apply"] == "skipped"
    ex = next(e for e in t2["executions"] if e["agent"] == "code-generation")
    assert ex["status"] == "failed"


def test_standalone_test_review_and_document_tasks(services, tmp_path):
    root, pid = mkcalc(services, tmp_path)
    configure(services, type("F", (), {"url": "http://127.0.0.1:1"})())
    t = run_task(services, "test", "Run the tests", pid, {"agents": ["unit-testing", "integration-testing"]})
    assert (
        t["status"] == "verified"
        and t["result"]["gate"]["passed"] == ["unit-testing"]
        and t["result"]["gate"]["skipped"] == ["integration-testing"]
    )
    r = run_task(services, "review", "Review", pid, {"files": [{"path": "x.py", "content": "eval(input())\n"}]})
    assert (r["status"] == "failed" and "PV-EVAL" in json.dumps(r["executions"])) or r["status"] == "failed"
    d = run_task(services, "document", "Document the schema", pid, {"doc_kind": "database"})
    assert d["status"] == "completed" and steps(d)["document"] == "success"


def test_unknown_kind_and_missing_project(services):
    async def go(req):
        return await services.orchestrator.submit(req)

    with pytest.raises(ValueError):
        asyncio.run(go(TaskRequest("nonsense", "x", None)))
    with pytest.raises(LookupError):
        asyncio.run(go(TaskRequest("generate", "x", 999)))
    with pytest.raises(ValueError, match="needs a project"):
        asyncio.run(go(TaskRequest("generate", "x", None)))


def test_executions_are_persisted_for_the_agent_control_centre(services, tmp_path):
    root, pid = mkcalc(services, tmp_path)
    with FakeOllama(responder=lambda *a: blocks(GOOD_CALC)) as fake:
        configure(services, fake)
        run_task(services, "generate", "Add multiply to calc.py", pid)
    desc = {a["key"]: a for a in services.agents.describe()}
    assert (
        desc["code-generation"]["last_execution"]["status"] == "success"
        and desc["unit-testing"]["last_execution"]
        and desc["code-generation"]["enabled"]
    )
    assert len(desc) == 20 and desc["orchestrator"]["category"] == "orchestration"
    with services.session_factory() as db:
        assert db.query(AgentExecution).count() >= 6 and Path(root).exists()
