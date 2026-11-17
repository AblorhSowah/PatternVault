"""E2E: the chat in real Chromium against the real backend + an Ollama protocol double (model output is scripted).
Verifies the UI/backend contract: streaming and recovery, citations, slash commands, history, honest no-model behaviour,
inertness of hostile answers, and that chat never writes to the project. Model *quality* is out of scope."""

import re
import time

import pytest
from playwright.sync_api import expect
from tests.conftest import AUTH_MODULE, FAKE_AWS, apply_auth_feature, run_git
from tests.helpers.fake_ollama import FakeOllama

pytestmark = pytest.mark.e2e

CODE = 'def create_refresh_token(user_id: int) -> str:\n    return "token"'


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
    state = {
        "answer": f"Reuse the JWT helper [#{patt}] like this:\n\n```python\n{CODE}\n```\n\nKeep the secret in an **environment variable**."
    }
    delay = {"s": 0.0}

    def responder(model, msgs, opts):
        time.sleep(delay["s"])
        return state["answer"]

    with FakeOllama(responder=responder) as fake:
        backend.call(
            "PATCH", "/config", {"mode": "offline", "local_model": {"base_url": fake.url, "model": "qwen2.5-coder:7b"}}
        )
        yield {"pid": pid, "patt": patt, "root": root, "fake": fake, "state": state, "delay": delay}


def ask(wb, text):
    wb.page.fill("#chat-input", text)
    wb.page.press("#chat-input", "Enter")


def open_chat(wb):
    wb.open()
    wb.nav("chat")
    expect(wb.page.locator("#chat-model-status")).to_contain_text("Local model ready: qwen2.5-coder:7b")


def test_ask_streams_an_answer_with_code_citation_and_sources(wb, backend, world):
    open_chat(wb)
    wb.page.select_option("#chat-project", str(world["pid"]))
    ask(wb, "How did we implement jwt login with password hashing?")
    expect(wb.page.locator(".msg.user").first).to_contain_text("How did we implement jwt login")
    answer = wb.page.locator(".msg.assistant:not(.live)").last
    expect(answer).to_contain_text("Reuse the JWT helper", timeout=30000)
    expect(answer.locator("pre.code code")).to_have_text(CODE)
    expect(answer.locator("strong")).to_have_text("environment variable")
    expect(answer.locator("button.cite").first).to_have_text(f"#{world['patt']}")  # inline citation is a known source
    expect(answer.locator(".msg-sources")).to_contain_text("Authentication")
    expect(wb.page.locator("#chat-conv option:checked")).to_contain_text("How did we implement jwt login")
    expect(wb.page.locator("#chat-send")).to_have_text("Send")
    assert world["fake"].chats()[-1]["messages"][0]["role"] == "system"
    assert (world["root"] / "app/auth.py").read_text() == AUTH_MODULE  # chatting changed nothing
    wb.shot("chat")
    assert wb.errors == []


def test_citation_and_source_chips_open_the_stored_pattern(wb, backend, world):
    open_chat(wb)
    ask(wb, "jwt login password hashing")
    answer = wb.page.locator(".msg.assistant:not(.live)").last
    expect(answer).to_contain_text("Reuse the JWT helper", timeout=30000)
    answer.locator("button.cite").first.click()
    card = wb.page.locator(".pattern-card")
    expect(card).to_contain_text("Authentication")
    expect(card.locator("details").first).to_be_visible()


def test_code_block_copy_and_open_in_editor_go_through_the_host(wb, backend, world):
    open_chat(wb)
    ask(wb, "jwt login")
    block = wb.page.locator(".codeblock").first
    expect(block).to_be_visible(timeout=30000)
    block.get_by_role("button", name="Copy").click()
    block.get_by_role("button", name="Open in editor").click()
    expect(block.get_by_role("button", name="Copied")).to_be_visible()
    assert wb.copied == [CODE]
    assert wb.opened == [(CODE, "python")]


def test_live_events_apply_in_order_without_duplicates_then_the_stored_answer_replaces_them(wb, backend, world):
    world["delay"]["s"] = 3.0
    world["state"]["answer"] = "final stored answer"
    open_chat(wb)
    ask(wb, "tell me something about jwt")
    live = wb.page.locator(".msg.assistant.live")
    expect(live).to_be_visible()
    turn = int(live.get_attribute("data-turn"))
    expect(live).to_contain_text("Thinking")

    def delta(text, offset):
        wb.host_event({"type": "chat.turn.delta", "data": {"turn_id": turn, "delta": text, "offset": offset}})

    delta("Hello ", 0)
    expect(live).to_contain_text("Hello")
    delta("world", 6)
    delta("world", 6)  # a replayed event must not duplicate text
    delta("Hello ", 0)  # nor an old one
    expect(live.locator(".md p")).to_have_text("Hello world")
    delta("lost", 500)  # a gap triggers a resync from the server instead of corrupting the text
    expect(live.locator(".md p")).to_have_text("Hello world")
    wb.host_event({"type": "chat.turn.delta", "data": {"turn_id": turn + 99, "delta": "other turn", "offset": 11}})
    expect(live.locator(".md p")).to_have_text("Hello world")  # other turns are ignored
    final = wb.page.locator(".msg.assistant:not(.live)").last
    expect(final).to_contain_text("final stored answer", timeout=30000)
    expect(wb.page.locator(".msg.assistant.live")).to_have_count(0)
    expect(wb.page.locator("#chat-send")).to_have_text("Send")


def test_stop_button_cancels_and_keeps_the_conversation_usable(wb, backend, world):
    world["delay"]["s"] = 4.0
    open_chat(wb)
    ask(wb, "a slow question about jwt")
    expect(wb.page.locator("#chat-send")).to_have_text("Stop")
    wb.page.click("#chat-send")
    expect(wb.page.locator(".msg.assistant:not(.live)").last).to_contain_text("stopped", timeout=15000)
    expect(wb.page.locator("#chat-send")).to_have_text("Send")
    world["delay"]["s"] = 0.0
    world["state"]["answer"] = "back again"
    ask(wb, "next question")
    expect(wb.page.locator(".msg.assistant:not(.live)").last).to_contain_text("back again", timeout=30000)


def test_history_new_chat_switching_and_delete(wb, backend, world):
    open_chat(wb)
    ask(wb, "first topic jwt")
    expect(wb.page.locator(".msg.assistant:not(.live)").last).to_contain_text("Reuse the JWT helper", timeout=30000)
    wb.page.click("#chat-new")
    expect(wb.page.locator(".welcome")).to_be_visible()
    expect(wb.page.locator(".msg")).to_have_count(0)
    world["state"]["answer"] = "second answer"
    ask(wb, "second topic")
    expect(wb.page.locator(".msg.assistant:not(.live)").last).to_contain_text("second answer", timeout=30000)
    options = wb.page.locator("#chat-conv option")
    expect(options).to_have_count(3)  # (new chat) + two conversations
    first_id = backend.call("GET", "/chat/conversations").json()[-1]["id"]
    wb.page.select_option("#chat-conv", str(first_id))
    expect(wb.page.locator(".msg.user").first).to_contain_text("first topic jwt")
    expect(wb.page.locator(".msg.assistant").last).to_contain_text("Reuse the JWT helper")
    wb.page.click("#chat-delete")
    expect(wb.page.locator("#chat-conv option")).to_have_count(2)
    assert not any(c["id"] == first_id for c in backend.call("GET", "/chat/conversations").json())
    # the history is stored in the backend, so a reload shows it again
    wb.page.reload()
    wb.page.wait_for_selector("#content")
    wb.nav("chat")
    expect(wb.page.locator(".msg.user").first).to_contain_text("second topic")


def test_slash_commands(wb, backend, world):
    open_chat(wb)
    wb.page.select_option("#chat-project", str(world["pid"]))
    ask(wb, "/help")
    expect(wb.page.locator(".msg.system dl.kv").first).to_contain_text("/search")
    ask(wb, "/nonsense")
    expect(wb.page.locator(".msg.system").last).to_contain_text("Unknown command")
    ask(wb, "/search jwt login password")
    results = wb.page.locator("#chat-search-results")
    expect(results).to_contain_text("Authentication")
    results.get_by_role("button", name="Show").first.click()
    expect(wb.page.locator(".pattern-card").last).to_contain_text("Authentication")
    results.get_by_role("button", name="Ask about this").first.click()
    expect(wb.page.locator("#chat-input")).to_have_value(re.compile("Explain pattern #"))
    wb.page.fill("#chat-input", "")
    ask(wb, "/status")
    expect(wb.page.locator("#chat-status-card")).to_contain_text("offline")
    expect(wb.page.locator("#chat-status-card")).to_contain_text("ready: qwen2.5-coder:7b")
    ask(wb, "/context add refresh tokens to the jwt login")
    expect(wb.page.locator("#context-report")).to_contain_text("Request understood as")
    assert backend.call("GET", "/chat/conversations").json() == []  # commands are not conversations
    assert wb.errors == []


def test_generate_command_proposes_without_writing(wb, backend, world):
    world["state"]["answer"] = (
        '<<<FILE path="app/auth.py" action="modify">>>\n'
        "import jwt\n\n\ndef create_refresh_token(user_id: int) -> str:\n"
        '    return jwt.encode({"sub": str(user_id)}, "change-me", algorithm="HS256")\n'
        "<<<END FILE>>>\n<<<EXPLANATION>>>\nAdded refresh tokens.\n<<<END EXPLANATION>>>"
    )
    open_chat(wb)
    wb.page.select_option("#chat-project", str(world["pid"]))
    ask(wb, "/generate add refresh tokens to the jwt login")
    result = wb.page.locator("#chat-generate-result")
    expect(result).to_contain_text("Added refresh tokens", timeout=30000)
    expect(result).to_contain_text("Nothing was written")
    assert not (world["root"] / "app/auth.py").read_text().count("create_refresh_token")


def test_generate_and_task_need_a_project(wb, backend, world):
    open_chat(wb)
    expect(wb.page.locator("#chat-project")).to_have_value(str(world["pid"]))  # the only project is preselected
    wb.page.select_option("#chat-project", "")
    for cmd in ("/generate add a feature", "/task add a feature", "/recreate 1"):
        ask(wb, cmd)
        expect(wb.page.locator(".msg.system .note").last).to_contain_text("Choose a project first")
    assert backend.call("GET", "/tasks").json() == []


def test_task_command_starts_an_orchestrated_task_not_a_direct_write(wb, backend, world):
    open_chat(wb)
    wb.page.select_option("#chat-project", str(world["pid"]))
    ask(wb, "/task add refresh tokens to the jwt login")
    card = wb.page.locator(".task-card")
    expect(card).to_be_visible(timeout=30000)
    expect(card).to_contain_text("generate")
    tasks = backend.call("GET", "/tasks").json()
    assert len(tasks) == 1 and tasks[0]["kind"] == "generate"
    backend.call("POST", f"/tasks/{tasks[0]['id']}/cancel")


def test_without_a_model_the_chat_says_so_and_answers_from_stored_patterns(wb, backend, world):
    backend.call("PATCH", "/config", {"local_model": {"base_url": "http://127.0.0.1:1"}})
    wb.open()
    wb.nav("chat")
    expect(wb.page.locator("#chat-model-status")).to_contain_text("No local model is ready")
    ask(wb, "jwt login with password hashing")
    answer = wb.page.locator(".msg.assistant:not(.live)").last
    expect(answer).to_contain_text("can't reach a language model", timeout=30000)
    expect(answer).to_contain_text("Authentication")
    expect(answer.locator(".msg-meta")).to_contain_text("no language model was used")
    assert world["fake"].chats() == []


def test_secrets_in_a_message_are_redacted_and_the_user_is_told(wb, backend, world):
    open_chat(wb)
    ask(wb, f"why does aws_access_key_id {FAKE_AWS} fail?")
    expect(wb.page.locator(".msg.user .note").first).to_contain_text("redacted", timeout=30000)
    expect(wb.page.locator(".msg.user").first).not_to_contain_text(FAKE_AWS)
    assert FAKE_AWS not in str(world["fake"].chats())


def test_hostile_answers_are_inert(wb, backend, world):
    world["state"]["answer"] = (
        '<img src=x onerror="window.__pwned=1"> <script>window.__pwned=2</script>\n\n'
        "[click me](javascript:window.__pwned=3) https://evil.example/x\n\nSee [#987654] for details."
    )
    open_chat(wb)
    ask(wb, "show me something")
    answer = wb.page.locator(".msg.assistant:not(.live)").last
    expect(answer).to_contain_text("click me", timeout=30000)
    assert wb.page.evaluate("window.__pwned") is None
    assert answer.locator("img, script, a, iframe").count() == 0  # rendered as text; no element, no link
    unknown = answer.locator(".cite.unknown")
    expect(unknown).to_have_text("#987654")  # a citation of a pattern that was never retrieved is marked, not trusted
    assert wb.errors == []


def test_host_prefill_fills_the_input_but_never_sends(wb, backend, world):
    open_chat(wb)
    wb.host_message({"type": "prefill", "text": "Explain this code from src/a.py (line 3)"})
    expect(wb.page.locator("#chat-input")).to_have_value("Explain this code from src/a.py (line 3)")
    assert backend.call("GET", "/chat/conversations").json() == []  # nothing was sent
    assert world["fake"].chats() == []


def test_sidebar_entry_point_is_chat_only_and_fits_a_narrow_view(wb, backend, world):
    wb.page.set_viewport_size({"width": 320, "height": 700})
    wb.open("chat.js")
    expect(wb.page.locator(".nav")).to_have_count(0)
    expect(wb.page.locator("#chat")).to_be_visible()
    ask(wb, "jwt login")
    expect(wb.page.locator(".msg.assistant:not(.live)").last).to_contain_text("Reuse the JWT helper", timeout=30000)
    overflow = wb.page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"horizontal overflow of {overflow}px at 320px wide"
    wb.shot("chat-sidebar")
    assert wb.errors == []


def test_backend_down_shows_a_helpful_retrying_message(wb, backend, world):
    wb.open("chat.js")
    backend.stop()
    wb.page.reload()
    expect(wb.page.locator("[role=alert]")).to_contain_text("could not reach the PatternVault backend", timeout=20000)
    expect(wb.page.get_by_role("button", name="Retry now")).to_be_visible()


def test_settings_can_delete_all_chat_history(wb, backend, world):
    open_chat(wb)
    ask(wb, "jwt login")
    expect(wb.page.locator(".msg.assistant:not(.live)").last).to_contain_text("Reuse the JWT helper", timeout=30000)
    ask(wb, "another question")
    assert backend.call("GET", "/chat/conversations").json() != []
    wb.nav("settings")
    wb.page.click("#btn-clear-chats")
    expect(wb.page.locator("#notice")).to_contain_text("Deleted 1 chat conversation")
    assert backend.call("GET", "/chat/conversations").json() == []
