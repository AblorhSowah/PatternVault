"""E2E: the Code Generation Workspace in real Chromium against the real backend + an Ollama protocol double.
Model output is scripted; this verifies the UI/backend contract, honesty of status messages and that nothing is applied."""

import pytest
from playwright.sync_api import expect
from tests.conftest import AUTH_MODULE, apply_auth_feature, run_git
from tests.helpers.fake_ollama import FakeOllama

pytestmark = pytest.mark.e2e

OUTPUT = """<<<FILE path="app/auth.py" action="modify">>>
import datetime

import jwt

SECRET = "change-me"


def create_access_token(user_id: int, minutes: int = 30) -> str:
    exp = datetime.datetime.utcnow() + datetime.timedelta(minutes=minutes)
    return jwt.encode({"sub": str(user_id), "exp": exp}, SECRET, algorithm="HS256")


def create_refresh_token(user_id: int) -> str:
    return jwt.encode({"sub": str(user_id), "typ": "refresh"}, SECRET, algorithm="HS256")
<<<END FILE>>>
<<<EXPLANATION>>>
Added refresh tokens next to the existing access token helper.
<<<END EXPLANATION>>>"""


@pytest.fixture()
def world(backend, tmp_path):
    root = tmp_path / "shop-api"
    (root / "app").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "requirements.txt").write_text("fastapi==0.110.0\n")
    (root / "app/main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "init")
    pid = backend.call("POST", "/projects", {"root_path": str(root)}).json()["id"]
    backend.call("POST", f"/projects/{pid}/analyze")
    sid = backend.call("POST", "/sessions", {"project_id": pid}).json()["id"]
    backend.call("POST", f"/sessions/{sid}/prompts", {"text": "Add JWT login with password authentication"})
    apply_auth_feature(root)
    patt = backend.call("POST", f"/sessions/{sid}/end", {}).json()["patterns"][0]["pattern_id"]
    with FakeOllama(responder=lambda *a: OUTPUT) as fake:
        backend.call(
            "PATCH", "/config", {"mode": "offline", "local_model": {"base_url": fake.url, "model": "qwen2.5-coder:7b"}}
        )
        yield pid, patt, root, fake


def test_context_generate_and_review_without_applying(wb, backend, world):
    pid, patt, root, fake = world
    wb.open()
    wb.nav("generate")
    expect(wb.page.locator("#model-status")).to_contain_text("Local model ready: qwen2.5-coder:7b")
    expect(wb.page.locator("#g-backend")).to_be_disabled()  # offline mode: cloud unavailable
    wb.page.select_option("#g-project", str(pid))
    wb.page.fill("#g-request", "Use the JWT authentication we built earlier but add support for refresh tokens")
    wb.page.click("#btn-analyze")
    expect(wb.page.locator("#context-report")).to_contain_text("adapt")
    expect(wb.page.locator("#context-report")).to_contain_text("Authentication")
    wb.page.click("#btn-generate")
    expect(wb.page.locator(".result-panel")).to_contain_text("Added refresh tokens", timeout=30000)
    expect(wb.page.locator(".diff-add").first).to_be_visible()
    expect(wb.page.locator(".result-panel")).to_contain_text("No problems detected")
    expect(wb.page.locator("#adaptation")).to_contain_text("adapted 1")
    assert (root / "app/auth.py").read_text() == AUTH_MODULE  # nothing was written to the project
    wb.shot("generate")
    assert wb.errors == []


def test_choose_knowledge_and_replay_plan(wb, backend, world):
    pid, patt, root, fake = world
    wb.open()
    wb.nav("generate")
    wb.page.select_option("#g-project", str(pid))
    wb.page.fill("#g-request", "jwt login password")
    wb.page.click("#btn-find")
    expect(wb.page.locator("#g-sources input[type=checkbox]").first).to_be_visible()
    wb.page.locator("#g-sources input[type=checkbox]").first.check()
    wb.page.select_option("#g-replay-pattern", str(patt))
    wb.page.click("#btn-replay")
    plan = wb.page.locator("#replay-plan")
    expect(plan).to_contain_text("compatible")
    expect(plan).to_contain_text("identical")  # the project already contains the replayed files
    expect(plan).to_contain_text("status is 'observed'")


def test_generate_reports_unreachable_model_honestly(wb, backend, world):
    pid, patt, root, fake = world
    backend.call("PATCH", "/config", {"local_model": {"base_url": "http://127.0.0.1:1"}})
    wb.open()
    wb.nav("generate")
    expect(wb.page.locator("#model-status")).to_contain_text("unreachable")
    wb.page.fill("#g-request", "Add a feature that does something")
    wb.page.click("#btn-generate")
    expect(wb.page.locator("#gen-output .error")).to_contain_text("cannot reach Ollama")
