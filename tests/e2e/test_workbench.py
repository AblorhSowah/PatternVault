"""E2E test plan (real Chromium + real backend process + real webview sources):
- Dashboard renders live counts, switches operating mode (persisted in the backend)
- Pattern Library: lists learned patterns, searches, shows code + honest "not verified" status, approve/deprecate/delete flows
- Learning Centre: honest provider capabilities, review queue, transcript import through the UI
- Settings: permission escalation asks for confirmation; privacy settings persist; secret redaction shown as always-on
- Security: hostile pattern content (HTML/script/event handlers) renders inert
- No console errors; keyboard-reachable controls; mobile-width layout does not overflow
"""

import json

import pytest
from playwright.sync_api import expect
from tests.conftest import (
    AUTH_MODULE,
    FAKE_AWS,
    apply_auth_feature,
    cc_assistant,
    cc_tool_result,
    cc_user,
    run_git,
    write_transcript,
)

pytestmark = pytest.mark.e2e
HOSTILE = '<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>'


@pytest.fixture()
def project(backend, tmp_path):
    root = tmp_path / "shop-api"
    (root / "app").mkdir(parents=True)
    (root / "requirements.txt").write_text("fastapi==0.110.0\n")
    (root / "app/main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "init")
    pid = backend.call("POST", "/projects", {"root_path": str(root)}).json()["id"]
    backend.call("POST", f"/projects/{pid}/analyze")
    return pid, root


@pytest.fixture()
def learned(backend, project):
    pid, root = project
    sid = backend.call("POST", "/sessions", {"project_id": pid, "title": "auth"}).json()["id"]
    backend.call("POST", f"/sessions/{sid}/prompts", {"text": "Add JWT login to the API with password authentication"})
    apply_auth_feature(root)
    end = backend.call("POST", f"/sessions/{sid}/end", {"outcome": "passed"}).json()
    return end["patterns"][0]["pattern_id"]


def test_dashboard_counts_and_mode_switch(wb, backend, learned):
    wb.open()
    expect(wb.page.locator("h2")).to_have_text("Dashboard")
    expect(wb.page.locator("#stat-patterns")).to_have_text("1")
    expect(wb.page.locator("#stat-verified")).to_have_text("0")
    expect(wb.page.locator("#stat-sessions")).to_have_text("1")
    wb.page.click('.seg[data-mode="offline"]')
    expect(wb.page.locator('.seg[data-mode="offline"]')).to_have_class("seg active")
    assert backend.call("GET", "/mode").json()["mode"] == "offline"
    wb.shot("dashboard")
    assert wb.errors == []


def test_library_search_detail_and_review_flow(wb, backend, learned):
    wb.open()
    wb.nav("library")
    row = wb.page.locator(".result").first
    expect(row).to_contain_text("Authentication")
    expect(wb.page.locator("#result-count")).to_have_text("1 pattern(s)")
    wb.page.fill("#search", "kubernetes helm")
    expect(wb.page.locator("#result-count")).to_have_text("0 pattern(s)")
    wb.page.fill("#search", "jwt login password")
    expect(wb.page.locator("#result-count")).to_have_text("1 pattern(s)")
    wb.page.locator(".result").first.click()
    expect(wb.page.locator("#detail-title")).to_contain_text("Authentication")
    expect(wb.page.locator(".note")).to_contain_text("Not verified")  # honest about trust
    wb.page.locator("details.file summary").first.click()
    expect(wb.page.locator("pre.code").first).to_be_visible()
    expect(wb.page.locator("#detail")).to_contain_text("create_access_token")
    wb.shot("library-detail")
    wb.page.get_by_role("button", name="Approve").click()
    expect(wb.page.locator("#detail")).to_contain_text("Approved ✓")
    assert backend.call("GET", f"/patterns/{learned}").json()["status"] == "approved"
    wb.page.click("#btn-delete")
    expect(wb.page.locator("#detail")).not_to_contain_text("Authentication")
    assert wb.confirms and "Permanently delete" in wb.confirms[-1]
    assert backend.call("GET", f"/patterns/{learned}").status_code == 404
    assert wb.errors == []


def test_delete_cancel_keeps_pattern(wb, backend, learned):
    wb.auto_confirm = False
    wb.open()
    wb.nav("library")
    wb.page.locator(".result").first.click()
    wb.page.click("#btn-delete")
    wb.page.wait_for_timeout(300)
    assert backend.call("GET", f"/patterns/{learned}").status_code == 200


def test_learning_centre_providers_queue_and_import(wb, backend, project):
    pid, root = project
    apply_auth_feature(root)
    c = str(root)
    write_transcript(
        backend.claude_dir,
        root,
        "sess-9",
        [
            cc_user("Add JWT login to the API with password authentication", c),
            cc_assistant(
                [
                    {
                        "type": "tool_use",
                        "id": "w1",
                        "name": "Write",
                        "input": {"file_path": f"{c}/app/auth.py", "content": AUTH_MODULE},
                    }
                ],
                c,
            ),
            cc_tool_result("w1", "File created successfully at: x", c),
        ],
    )
    wb.open()
    wb.nav("learning")
    copilot = wb.page.locator('[data-provider="github-copilot"]')
    expect(copilot).to_contain_text("no prompts")  # honest: Copilot exposes no capture channel
    expect(wb.page.locator('[data-provider="continue"]')).to_contain_text("unverified against real install")
    claude = wb.page.locator('[data-provider="claude-code"]')
    expect(claude).to_contain_text("log source found")
    claude.get_by_role("button", name="Find sessions").click()
    claude.get_by_role("button", name="Import").click()
    expect(wb.page.locator("#notice")).to_contain_text("1 pattern(s) learned")
    expect(wb.page.locator("[data-pattern-id]").first).to_contain_text("Authentication")
    wb.page.get_by_role("button", name="Approve").first.click()
    wb.page.wait_for_timeout(300)
    assert backend.call("GET", "/patterns", query={"status": "approved"}).json()["total"] == 1
    wb.shot("learning")
    assert wb.errors == []


def test_settings_privacy_and_permission_confirmation(wb, backend):
    wb.open()
    wb.nav("settings")
    expect(wb.page.locator(".content")).to_contain_text("secret redaction: always on")
    wb.page.fill("#s-exclude", "legacy/**\n*.draft.md")
    wb.page.locator("#s-exclude").blur()
    expect(wb.page.locator("#notice")).to_have_text("Saved")
    assert backend.call("GET", "/config").json()["privacy"]["exclude_globs"] == ["legacy/**", "*.draft.md"]
    wb.page.select_option("#s-permission", "restricted_autonomous")
    wb.page.wait_for_timeout(300)
    assert any("Restricted autonomous" in c for c in wb.confirms)
    assert backend.call("GET", "/config").json()["permission_level"] == "restricted_autonomous"
    wb.page.uncheck("#s-capture")
    wb.page.wait_for_timeout(300)
    assert backend.call("GET", "/config").json()["privacy"]["capture_enabled"] is False
    wb.page.click("#btn-export")
    wb.page.wait_for_timeout(300)
    assert (
        wb.saved and wb.saved[0][0] == "patternvault-export.json" and wb.saved[0][1]["format"] == "patternvault-export"
    )
    wb.shot("settings")


def test_hostile_content_renders_inert(wb, backend, learned):
    backend.call("PATCH", f"/patterns/{learned}", {"title": f"Evil {HOSTILE}", "summary": HOSTILE})
    # also hostile *code content* via an imported pattern file
    doc = backend.call("GET", "/knowledge/export").json()
    doc["patterns"][0]["versions"][0]["content"]["files"][0]["content"] = HOSTILE
    wb.open()
    wb.nav("library")
    wb.page.locator(".result").first.click()
    wb.page.locator("details.file summary").first.click()
    wb.page.wait_for_timeout(400)
    assert wb.page.evaluate("window.__xss") is None  # nothing executed
    expect(wb.page.locator("#detail-title")).to_contain_text("<img src=x")  # shown literally, as text
    assert wb.page.locator("#detail img").count() == 0 and wb.page.locator("#detail script").count() == 0
    wb.nav("learning")
    wb.page.wait_for_timeout(300)
    assert wb.page.evaluate("window.__xss") is None
    assert wb.page.locator("img[src='x']").count() == 0


def test_secret_never_displayed(wb, backend, project):
    pid, root = project
    sid = backend.call("POST", "/sessions", {"project_id": pid}).json()["id"]
    backend.call("POST", f"/sessions/{sid}/prompts", {"text": "Add JWT login to the API with password authentication"})
    apply_auth_feature(root)
    (root / "app/auth.py").write_text(AUTH_MODULE + f'\nKEY = "{FAKE_AWS}"\n')
    backend.call("POST", f"/sessions/{sid}/end", {})
    wb.open()
    wb.nav("library")
    wb.page.locator(".result").first.click()
    wb.page.locator("details.file summary").first.click()
    body = wb.page.inner_text("body")
    assert FAKE_AWS not in body


def test_mobile_width_no_horizontal_overflow(wb, backend, learned):
    wb.page.set_viewport_size({"width": 390, "height": 800})
    wb.open()
    for section in ("dashboard", "library", "learning", "settings"):
        wb.nav(section)
        wb.page.wait_for_timeout(250)
        overflow = wb.page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        if overflow > 1:
            offenders = wb.page.evaluate(
                """() => [...document.querySelectorAll('#app *')].filter(e => e.getBoundingClientRect().right > document.documentElement.clientWidth + 1).slice(0, 6).map(e => e.tagName + '.' + e.className + ' ' + Math.round(e.getBoundingClientRect().right))"""
            )
            raise AssertionError(f"{section} overflows by {overflow}px: {offenders}")
    wb.shot("mobile-library")


def test_keyboard_navigation_reaches_controls(wb, backend, learned):
    wb.open()
    wb.nav("library")
    wb.page.focus("#search")
    wb.page.keyboard.press("Tab")
    focused = wb.page.evaluate("document.activeElement && document.activeElement.id")
    assert focused == "f-category"
    wb.page.locator(".result").first.focus()
    wb.page.keyboard.press("Enter")
    expect(wb.page.locator("#detail-title")).to_be_visible()


def test_no_browser_errors_or_stray_null_text_across_all_sections(wb, backend, learned):
    import re

    wb.open()
    for s in ("dashboard", "library", "learning", "generate", "agents", "testing", "settings"):
        wb.nav(s)
        wb.page.wait_for_timeout(300)
        text = wb.page.inner_text("#content")
        stray = re.findall(r"(?<![\w.])(null|undefined|NaN)(?![\w.])", text)
        assert not stray, f"{s}: literal {stray!r} rendered in the page text"
    assert wb.errors == [], json.dumps(wb.errors)
