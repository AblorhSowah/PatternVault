"""Test plan: learning/adapters
Claude Code (synthetic transcripts in the real on-disk schema):
- discovery by encoded project dir; metadata (title, cwd, branch, counts)
- user prompts extracted; <system-reminder> stripped; tool_result-only turns are not prompts; command wrappers ignored
- assistant text grouped by message id; **thinking blocks never captured** (and counted in `skipped`)
- Write/Edit/MultiEdit/Bash mapped to FileEdit/ShellCommand with results (created vs updated, is_error)
- sidechain text excluded but sidechain edits kept; malformed lines tolerated
Continue: sessions parsed (list/str content); other workspaces filtered out
Unsupported providers: honest capabilities, UnsupportedOperation on load
"""

import json
from pathlib import Path

import pytest

from app.learning.adapters.base import UnsupportedOperation
from app.learning.adapters.claude_code import ClaudeCodeAdapter, encode_project_dir
from app.learning.adapters.continue_dev import ContinueAdapter
from app.learning.adapters.registry import AdapterRegistry
from tests.conftest import cc_assistant, cc_tool_result, cc_user, write_transcript

pytestmark = pytest.mark.unit

SECRET_THOUGHT = "PRIVATE-CHAIN-OF-THOUGHT-should-never-be-stored"


@pytest.fixture()
def proj(tmp_path):
    p = tmp_path / "my-app"
    p.mkdir()
    return p


def transcript(proj):
    c = str(proj)
    return [
        {"type": "ai-title", "aiTitle": "Add login", "sessionId": "s1"},
        {"type": "system", "subtype": "x"},
        cc_user(
            "<system-reminder>ignore me</system-reminder>\nAdd a login endpoint with JWT", c, gitBranch="feat/login"
        ),
        cc_assistant(
            [{"type": "thinking", "thinking": SECRET_THOUGHT}, {"type": "text", "text": "I'll create the module."}],
            c,
            mid="m1",
        ),
        cc_assistant(
            [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Write",
                    "input": {"file_path": f"{c}/app/auth.py", "content": "def login():\n    return 1\n"},
                }
            ],
            c,
            mid="m1",
        ),
        cc_tool_result("t1", f"File created successfully at: {c}/app/auth.py", c),
        cc_assistant(
            [
                {
                    "type": "tool_use",
                    "id": "t2",
                    "name": "Edit",
                    "input": {"file_path": f"{c}/app/auth.py", "old_string": "return 1", "new_string": "return 2"},
                }
            ],
            c,
            mid="m2",
        ),
        cc_tool_result("t2", f"The file {c}/app/auth.py has been updated.", c),
        cc_assistant(
            [
                {
                    "type": "tool_use",
                    "id": "t3",
                    "name": "Bash",
                    "input": {"command": "pytest -q", "description": "run tests"},
                }
            ],
            c,
            mid="m3",
        ),
        cc_tool_result("t3", "2 passed", c),
        cc_assistant([{"type": "tool_use", "id": "t4", "name": "Bash", "input": {"command": "ls"}}], c, mid="m4"),
        cc_tool_result("t4", "Exit code 1\nboom", c, is_error=True),
        cc_assistant(
            [{"type": "tool_use", "id": "t5", "name": "Write", "input": {"file_path": f"{c}/bad.py", "content": "x"}}],
            c,
            mid="m5",
        ),
        cc_tool_result("t5", "permission denied", c, is_error=True),
        cc_assistant(
            [
                {"type": "text", "text": "subagent chatter"},
                {
                    "type": "tool_use",
                    "id": "t6",
                    "name": "Write",
                    "input": {"file_path": f"{c}/side.py", "content": "y = 1\n"},
                },
            ],
            c,
            mid="m6",
            sidechain=True,
        ),
        cc_tool_result("t6", "File created successfully at: x", c),
        cc_user("<command-name>/clear</command-name>", c),
        cc_user("Thanks, now add logout", c, ts="2026-01-01T10:05:00.000Z"),
        cc_assistant([{"type": "text", "text": "Done."}], c, mid="m7", ts="2026-01-01T10:05:09.000Z"),
    ]


def test_encode_project_dir():
    assert encode_project_dir("/Users/a.b/My_proj") == "-Users-a-b-My-proj"


def test_discover_metadata(claude_dir, proj):
    write_transcript(claude_dir, proj, "s1", transcript(proj))
    write_transcript(claude_dir, proj.parent / "other", "zz", [cc_user("hi", "/x", sid="zz")])
    ad = ClaudeCodeAdapter(claude_dir)
    assert ad.available()
    found = ad.discover(proj)
    assert len(found) == 1
    s = found[0]
    assert (s.external_id, s.title, s.git_branch, s.cwd) == ("s1", "Add login", "feat/login", str(proj))
    assert s.message_count > 5 and s.started_at < s.ended_at
    assert len(ad.discover(None)) == 2


def test_load_prompts_and_text(claude_dir, proj):
    f = write_transcript(claude_dir, proj, "s1", transcript(proj))
    ad = ClaudeCodeAdapter(claude_dir)
    cap = ad.load(ad.discover(proj)[0])
    users = [m.text for m in cap.messages if m.role == "user"]
    assert users == [
        "Add a login endpoint with JWT",
        "Thanks, now add logout",
    ]  # reminders/commands/tool results excluded
    assistants = [m for m in cap.messages if m.role == "assistant"]
    assert assistants[0].text == "I'll create the module." and assistants[0].model == "claude-test"
    assert "subagent chatter" not in " ".join(m.text for m in assistants)
    assert f.exists()


def test_thinking_blocks_never_captured(claude_dir, proj):
    write_transcript(claude_dir, proj, "s1", transcript(proj))
    ad = ClaudeCodeAdapter(claude_dir)
    cap = ad.load(ad.discover(proj)[0])
    blob = json.dumps([m.__dict__ for m in cap.messages], default=str)
    assert SECRET_THOUGHT not in blob
    assert cap.skipped["thinking_blocks"] == 1 and cap.skipped["sidechain_text"] == 1


def test_file_edits_and_commands(claude_dir, proj):
    write_transcript(claude_dir, proj, "s1", transcript(proj))
    ad = ClaudeCodeAdapter(claude_dir)
    cap = ad.load(ad.discover(proj)[0])
    by_path = {}
    for e in cap.file_edits:
        by_path.setdefault(Path(e.path).name, []).append(e)
    assert by_path["auth.py"][0].kind == "create" and by_path["auth.py"][0].content.startswith("def login")
    assert by_path["auth.py"][1].edits == [("return 1", "return 2")]
    assert by_path["bad.py"][0].ok is False  # failed write recorded as failed
    assert "side.py" in by_path  # sidechain edits are real work
    cmds = {c.command: c for c in cap.commands}
    assert cmds["pytest -q"].is_test and cmds["pytest -q"].ok is True
    assert cmds["ls"].ok is False and not cmds["ls"].is_test


def test_malformed_lines_and_empty_session(claude_dir, proj):
    d = claude_dir / "projects" / encode_project_dir(proj.resolve())
    d.mkdir(parents=True)
    (d / "bad.jsonl").write_text(
        "{not json\n\n" + json.dumps(cc_user("hello there", str(proj), sid="bad")) + "\n[1,2]\n"
    )
    (d / "empty.jsonl").write_text(json.dumps({"type": "system"}) + "\n")
    ad = ClaudeCodeAdapter(claude_dir)
    found = ad.discover(proj)
    assert [s.external_id for s in found] == ["bad"]  # empty session skipped
    assert ad.load(found[0]).messages[0].text == "hello there"


def test_unavailable_when_dir_missing(tmp_path):
    ad = ClaudeCodeAdapter(tmp_path / "nope")
    assert not ad.available() and ad.discover() == []


def test_continue_adapter(continue_dir, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (continue_dir / "sessions" / "a1.json").write_text(
        json.dumps(
            {
                "sessionId": "a1",
                "title": "Refactor",
                "workspaceDirectory": str(ws),
                "history": [
                    {"message": {"role": "user", "content": "refactor x"}},
                    {"message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}},
                    {"message": {"role": "system", "content": "hidden"}},
                ],
            }
        )
    )
    (continue_dir / "sessions" / "b1.json").write_text(
        json.dumps({"sessionId": "b1", "workspaceDirectory": "/elsewhere", "history": []})
    )
    (continue_dir / "sessions" / "sessions.json").write_text("[]")
    ad = ContinueAdapter(continue_dir)
    found = ad.discover(ws)
    assert [s.external_id for s in found] == ["a1"]
    cap = ad.load(found[0])
    assert [(m.role, m.text) for m in cap.messages] == [("user", "refactor x"), ("assistant", "ok")]
    assert ad.capabilities.verified_against_real_install is False and not ad.capabilities.captures_file_edits


def test_unsupported_providers_are_honest(claude_dir, continue_dir):
    reg = AdapterRegistry(claude_dir, continue_dir)
    for key in ("github-copilot", "cursor"):
        a = reg.get(key)
        assert not (
            a.capabilities.captures_prompts or a.capabilities.captures_responses or a.capabilities.captures_file_edits
        )
        assert not a.available()
        with pytest.raises(UnsupportedOperation):
            a.discover()
    assert reg.get("claude-code").capabilities.verified_against_real_install is True
    assert reg.get("manual").available()
    with pytest.raises(KeyError):
        reg.get("nope")
