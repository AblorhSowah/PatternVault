"""E2E: Settings + Dashboard completeness in real Chromium against the real backend (+ Ollama protocol double).
Local model selection/test, resource limits, provider/cloud consent, storage limits + GC, encrypted backup & restore (incl. wrong
passphrase), audit log + chain verification, dashboard model/resource cards."""

import pytest
from playwright.sync_api import expect
from tests.conftest import apply_auth_feature, run_git
from tests.helpers.fake_ollama import FakeOllama

pytestmark = pytest.mark.e2e
PASS = "correct horse battery staple"


@pytest.fixture()
def learned(backend, tmp_path):
    root = tmp_path / "shop"
    (root / "app").mkdir(parents=True)
    (root / "requirements.txt").write_text("fastapi==0.110.0\n")
    (root / "app/main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "init")
    pid = backend.call("POST", "/projects", {"root_path": str(root)}).json()["id"]
    sid = backend.call("POST", "/sessions", {"project_id": pid}).json()["id"]
    backend.call("POST", f"/sessions/{sid}/prompts", {"text": "Add JWT login with password authentication"})
    apply_auth_feature(root)
    return backend.call("POST", f"/sessions/{sid}/end", {}).json()["patterns"][0]["pattern_id"]


def test_dashboard_shows_model_and_resource_cards(wb, backend):
    wb.open()
    expect(wb.page.locator("#dash-model")).to_contain_text("unreachable")
    expect(wb.page.locator("#dash-resources")).to_contain_text("cores")
    with FakeOllama(["qwen2.5-coder:7b"]) as fake:
        backend.call("PATCH", "/config", {"local_model": {"base_url": fake.url, "model": "qwen2.5-coder:7b"}})
        wb.nav("settings")
        wb.nav("dashboard")
        expect(wb.page.locator("#dash-model")).to_contain_text("ready")
        expect(wb.page.locator("#dash-model")).to_contain_text("Ollama 0.0.0-double")
        wb.shot("dashboard-full")


def test_settings_model_selection_and_resource_limits(wb, backend):
    with FakeOllama(["qwen2.5-coder:7b", "nomic-embed-text"]) as fake:
        backend.call("PATCH", "/config", {"local_model": {"base_url": fake.url}})
        wb.open()
        wb.nav("settings")
        expect(wb.page.locator("#s-model-status")).to_contain_text("no local model selected")
        wb.page.select_option("#s-model", "qwen2.5-coder:7b")
        wb.page.click("#btn-use-model")
        expect(wb.page.locator("#notice")).to_contain_text("Using qwen2.5-coder:7b")
        expect(wb.page.locator("#s-model-status")).to_contain_text("ready")
        wb.page.click("#btn-test-model")
        expect(wb.page.locator("#notice")).to_contain_text("Model replied “OK”")
        for sel, val in (("#s-threads", "2"), ("#s-gpu", "0"), ("#s-ctx", "4096"), ("#s-maxout", "2048")):
            wb.page.fill(sel, val)
            wb.page.locator(sel).blur()
            expect(wb.page.locator("#notice")).to_have_text("Saved")
        wb.page.fill("#s-keepalive", "10m")
        wb.page.locator("#s-keepalive").blur()
        expect(wb.page.locator("#notice")).to_have_text("Saved")
        lm = backend.call("GET", "/config").json()["local_model"]
        assert (
            lm["model"],
            lm["num_thread"],
            lm["num_gpu"],
            lm["num_ctx"],
            lm["max_answer_length"],
            lm["keep_alive"],
        ) == ("qwen2.5-coder:7b", 2, 0, 4096, 2048, "10m")
        wb.page.fill("#s-pull-name", "qwen2.5-coder:1.5b")
        wb.page.click("#btn-pull")
        expect(wb.page.locator("#notice")).to_contain_text("Downloading qwen2.5-coder:1.5b")
        wb.shot("settings-model")
        assert wb.errors == []


def test_settings_providers_cloud_consent_and_storage(wb, backend):
    wb.open()
    wb.nav("settings")
    expect(wb.page.locator('[data-provider="claude-code"]')).to_be_visible()
    expect(wb.page.locator('[data-provider="github-copilot"]')).to_contain_text("workspace capture only")
    wb.page.locator('[data-provider="claude-code"] input[type=checkbox]').uncheck()
    expect(wb.page.locator("#notice")).to_have_text("Saved")
    assert backend.call("GET", "/config").json()["providers"]["claude-code"]["enabled"] is False
    wb.page.check("#s-cloud-consent")
    expect(wb.page.locator("#notice")).to_have_text("Cloud consent recorded")
    wb.page.fill("#s-cloud-url", "https://api.example.com/v1")
    wb.page.locator("#s-cloud-url").blur()
    expect(wb.page.locator("#notice")).to_have_text("Saved")
    cloud = backend.call("GET", "/config").json()["cloud"]
    assert (
        cloud["consent"] == {"openai-compatible": True}
        and cloud["openai_compatible_base_url"] == "https://api.example.com/v1"
    )
    assert "api_key" not in str(backend.call("GET", "/config").json()).lower()  # keys never live in config
    wb.page.fill("#s-max-blobs", "512")
    wb.page.locator("#s-max-blobs").blur()
    expect(wb.page.locator("#s-usage")).to_contain_text("/ 512 MB")
    wb.page.click("#btn-gc")
    expect(wb.page.locator("#notice")).to_contain_text("unreferenced blob")
    wb.page.click("#btn-set-key")
    wb.page.wait_for_timeout(200)
    assert "patternvault.setProviderKey" in wb.commands


def test_encrypted_backup_and_restore_through_the_ui(wb, backend, learned):
    wb.open()
    wb.nav("settings")
    wb.page.fill("#s-backup-pass", "short")
    wb.page.click("#btn-backup")
    expect(wb.page.locator("#notice")).to_contain_text("at least 8")
    wb.page.fill("#s-backup-pass", PASS)
    wb.page.click("#btn-backup")
    expect(wb.page.locator("#notice")).to_contain_text("(encrypted)")
    row = wb.page.locator("#s-backups [data-backup]").first
    expect(row).to_contain_text("encrypted")
    backend.call("DELETE", f"/patterns/{learned}", query={"confirm": True})
    assert backend.call("GET", "/patterns").json()["total"] == 0
    wb.page.fill("#s-restore-pass", "wrong passphrase!!")
    row.get_by_role("button", name="Restore…").click()
    expect(wb.page.locator("#notice")).to_contain_text("wrong passphrase")
    assert backend.call("GET", "/patterns").json()["total"] == 0  # a failed restore changed nothing
    wb.page.fill("#s-restore-pass", PASS)
    wb.page.locator("#s-backups [data-backup]").first.get_by_role("button", name="Verify").click()
    expect(wb.page.locator("#notice")).to_contain_text("Backup OK: 1 patterns")
    wb.page.fill("#s-restore-pass", PASS)
    wb.page.locator("#s-backups [data-backup]").first.get_by_role("button", name="Restore…").click()
    expect(wb.page.locator("#notice")).to_contain_text("Restored")
    assert backend.call("GET", "/patterns").json()["total"] == 1  # the deleted pattern is back
    wb.shot("settings-backup")
    wb.page.locator("#s-backups [data-backup]").first.get_by_role("button", name="Delete").click()
    expect(wb.page.locator("#notice")).to_contain_text("Backup deleted")


def test_audit_log_table_and_chain_verification(wb, backend, learned):
    wb.open()
    wb.nav("settings")
    expect(wb.page.locator("#s-audit tbody tr").first).to_be_visible()
    assert "pattern.created" in wb.page.inner_text("#s-audit")
    wb.page.click("#btn-verify-audit")
    expect(wb.page.locator("#notice")).to_contain_text("Audit chain intact")
