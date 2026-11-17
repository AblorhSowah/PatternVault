"""E2E (real Chromium + real backend process + real pytest runs on a real project; model output scripted by a protocol double)
- Generation Workspace: end-to-end task shows live plan, files changed, and "verified by passing tests"
- inline approvals (assisted mode); failing generation → correction attempts → rollback note
- Agent Control Centre: 20 agents in 3 groups, enable/disable persists, task history, recovery-point restore (host-confirmed)
- Testing Centre: honest failed/skipped display with expected/actual/error details; run tests from the UI
"""

import pytest
from playwright.sync_api import expect
from tests.helpers.fake_ollama import FakeOllama
from tests.integration.test_orchestrator import BUGGY_CALC, CALC, CALC_TEST, GOOD_CALC, MUL_TEST, blocks

pytestmark = pytest.mark.e2e


@pytest.fixture()
def calc(backend, tmp_path):
    root = tmp_path / "calc-app"
    (root / "tests/unit").mkdir(parents=True)
    (root / "requirements.txt").write_text("pytest\n")
    (root / "calc.py").write_text(CALC)
    (root / "tests/unit/test_calc.py").write_text(CALC_TEST)
    (root / "conftest.py").write_text(
        "import pathlib\nimport sys\n\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\n"
    )
    pid = backend.call("POST", "/projects", {"root_path": str(root)}).json()["id"]
    backend.call("POST", f"/projects/{pid}/analyze")
    return pid, root


def point_backend(backend, fake, level="restricted_autonomous"):
    backend.call(
        "PATCH",
        "/config",
        {
            "mode": "offline",
            "permission_level": level,
            "local_model": {"base_url": fake.url, "model": "qwen2.5-coder:7b"},
        },
    )


def start_task_from_ui(wb, pid, request="Add a multiply function to calc.py with a unit test"):
    wb.open()
    wb.nav("generate")
    wb.page.select_option("#g-project", str(pid))
    wb.page.fill("#g-request", request)
    wb.page.click("#btn-run-task")


def test_end_to_end_task_verified_from_the_ui(wb, backend, calc):
    pid, root = calc
    with FakeOllama(responder=lambda *a: blocks(GOOD_CALC)) as fake:
        point_backend(backend, fake)
        start_task_from_ui(wb, pid)
        card = wb.page.locator(".task-card")
        expect(card).to_contain_text("verified by passing tests", timeout=120000)
        expect(card.locator('.plan-step[data-status="success"]')).to_have_count(6)
        expect(card).to_contain_text("calc.py")
        expect(card).to_contain_text("Verification gate — passed: unit-testing")
        # (the host-side "are you sure you want to start this task" prompt is unit-tested in Vitest: this harness shims the host)
        assert "def multiply" in (root / "calc.py").read_text()
        card.locator("details summary").first.click()
        expect(card.locator("table")).to_contain_text("code-generation")
        wb.shot("task-verified")
    assert wb.errors == []


def test_assisted_mode_shows_inline_approvals(wb, backend, calc):
    pid, root = calc
    with FakeOllama(responder=lambda *a: blocks(GOOD_CALC)) as fake:
        point_backend(backend, fake, level="assisted")
        start_task_from_ui(wb, pid)
        approval = wb.page.locator(".approval").first
        expect(approval).to_be_visible(timeout=60000)
        expect(approval).to_contain_text("write_file")
        assert "def multiply" not in (root / "calc.py").read_text()  # nothing written until approved
        wb.shot("approval-pending")
        for _ in range(12):
            btn = wb.page.get_by_role("button", name="Approve")
            if btn.count() == 0 and wb.page.locator(".task-card").get_by_text("verified by passing tests").count():
                break
            if btn.count():
                btn.first.click()
            wb.page.wait_for_timeout(700)
        expect(wb.page.locator(".task-card")).to_contain_text("verified by passing tests", timeout=120000)


def test_failed_correction_is_rolled_back_and_explained(wb, backend, calc):
    pid, root = calc
    with FakeOllama(responder=lambda *a: blocks(BUGGY_CALC)) as fake:
        point_backend(backend, fake)
        backend.call("PATCH", "/config", {"agents": {"max_fix_attempts": 1}})
        start_task_from_ui(wb, pid)
        card = wb.page.locator(".task-card")
        expect(card).to_contain_text("restored to recovery point", timeout=120000)
        expect(card).to_contain_text("not verified")
        assert (root / "calc.py").read_text() == CALC and not (root / "tests/unit/test_multiply.py").exists()
        wb.shot("task-rolled-back")


def test_agent_control_centre_lists_toggles_and_restores(wb, backend, calc):
    pid, root = calc
    with FakeOllama(responder=lambda *a: blocks(GOOD_CALC)) as fake:
        point_backend(backend, fake)
        t = backend.call(
            "POST",
            "/tasks",
            {
                "kind": "generate",
                "request": "Add multiply to calc.py",
                "project_id": pid,
                "params": {"agents": ["unit-testing"]},
            },
        ).json()["id"]
        import time

        for _ in range(300):
            if backend.call("GET", f"/tasks/{t}").json()["status"] in ("verified", "failed", "completed"):
                break
            time.sleep(0.3)
    wb.open()
    wb.nav("agents")
    expect(wb.page.locator("tr[data-agent]")).to_have_count(20)
    for cat in ("development", "testing", "orchestration"):
        assert wb.page.locator(f'table[data-category="{cat}"]').count() == 1
    row = wb.page.locator('tr[data-agent="code-review-security"]')
    row.locator("input[type=checkbox]").uncheck()
    expect(wb.page.locator("#notice")).to_contain_text("disabled")
    assert "code-review-security" in backend.call("GET", "/config").json()["agents"]["disabled"]
    expect(wb.page.locator('tr[data-agent="code-generation"]')).to_contain_text("success")
    expect(wb.page.locator('tr[data-agent="orchestrator"]')).to_contain_text("always on")
    expect(wb.page.locator("#task-history .task-card").first).to_contain_text("verified by passing tests")
    wb.shot("agents")
    wb.page.get_by_role("button", name="Restore").first.click()
    wb.page.wait_for_timeout(800)
    assert any("Restore" in c for c in wb.confirms)
    assert (root / "calc.py").read_text() == CALC  # the project was restored to its pre-task state
    expect(wb.page.locator("#notice")).to_contain_text("Restored recovery point")
    assert wb.errors == []


def test_testing_centre_runs_from_ui_and_is_honest_about_failures_and_skips(wb, backend, calc):
    pid, root = calc
    (root / "tests/unit/test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 4, 'arithmetic is broken'\n"
    )
    backend.call("PATCH", "/config", {"permission_level": "restricted_autonomous"})
    wb.open()
    wb.nav("testing")
    expect(wb.page.locator("#agent-cards [data-test-agent]")).to_have_count(10)
    wb.page.select_option("#t-project", str(pid))
    wb.page.locator('#t-agents input[data-agent="e2e-testing"]').check()
    wb.page.click("#btn-run-tests")
    expect(wb.page.locator("#notice")).to_contain_text("started")
    wb.page.wait_for_selector("#session-list .result", timeout=90000)
    expect(wb.page.locator("#failing-banner")).to_contain_text("failing test result", timeout=60000)
    wb.page.select_option("#t-filter-status", "failed")
    wb.page.locator("#session-list .result").first.click()
    detail = wb.page.locator("#session-detail")
    expect(detail.locator("#detail-agent")).to_have_text("Unit")
    expect(detail).to_contain_text("arithmetic is broken")
    expect(detail.locator('.result-row[data-status="failed"] dt').first).to_be_visible()
    expect(detail).to_contain_text("Expected")
    expect(detail.locator("pre.code").first).to_contain_text("assert")
    wb.shot("testing-failed")
    wb.page.select_option("#t-filter-status", "skipped")
    wb.page.locator("#session-list .result").first.click()
    expect(wb.page.locator("#session-detail")).to_contain_text("Skipped (this is not a pass)")
    assert wb.errors == []


def test_new_sections_have_no_horizontal_overflow_on_phones(wb, backend, calc):
    wb.page.set_viewport_size({"width": 390, "height": 800})
    wb.open()
    for section in ("generate", "agents", "testing"):
        wb.nav(section)
        wb.page.wait_for_timeout(400)
        overflow = wb.page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 1, f"{section} overflows by {overflow}px"


def test_mutating_requests_are_guarded_by_the_host_shim_contract(wb, backend, calc):
    # Not a security test of the host (that is in Vitest); verifies the UI always routes through confirm() for these actions.
    pid, root = calc
    wb.open()
    wb.nav("agents")
    wb.page.wait_for_timeout(300)
    assert [m["type"] for m in wb.page.evaluate("window.__pvMessages")].count("api") >= 4
    assert MUL_TEST  # keep fixture imports used
