"""Test plan:
A. Importing a Claude Code transcript through the API
   - providers list shows honest capabilities; sessions discovered per project
   - import maps prompts/responses/file edits; excluded files (.env) dropped & counted; final content from disk
   - observed test run sets session.outcome but pattern stays `observed` (NOT verified)
   - re-import is idempotent unless force; unsupported providers → 422
B. Lifecycle verification gate (ADR-006)
   - `tested`/`verified` impossible without a passing testing session that executed tests
   - failed / empty / unknown sessions rejected and audited as denied
   - verified additionally needs user approval or successful reuse; new version demotes verification
   - confidence is monotonic in trust and capped below 0.8 unless verified
"""

import json

import pytest

from app.extraction.extractor import FileInput, SessionInput, extract_candidates
from app.knowledge.lifecycle import LifecycleError, compute_confidence
from app.models import TestingSession, TestResult
from tests.conftest import (
    AUTH_MODULE,
    AUTH_ROUTES,
    AUTH_TEST,
    FAKE_AWS,
    apply_auth_feature,
    cc_assistant,
    cc_tool_result,
    cc_user,
    write_transcript,
)

pytestmark = pytest.mark.integration
V1 = "/api/v1"


@pytest.fixture()
def project(api, auth_project):
    return api.post(f"{V1}/projects", json={"root_path": str(auth_project)}).json()["id"]


def make_transcript(claude_dir, root):
    c = str(root)

    def w(i, path, content):
        return cc_assistant(
            [{"type": "tool_use", "id": i, "name": "Write", "input": {"file_path": f"{c}/{path}", "content": content}}],
            c,
            mid=i,
        )

    def ok(i):
        return cc_tool_result(i, "File created successfully at: x", c)

    lines = [
        cc_user("Add JWT login to the API. Users authenticate with a password.", c),
        cc_assistant(
            [{"type": "thinking", "thinking": "SECRET-THOUGHT"}, {"type": "text", "text": "Creating the auth module."}],
            c,
            mid="a0",
        ),
        w("t1", "app/auth.py", AUTH_MODULE),
        ok("t1"),
        w("t2", "app/routes/auth_routes.py", AUTH_ROUTES),
        ok("t2"),
        w("t3", "tests/test_auth.py", AUTH_TEST),
        ok("t3"),
        w("t4", ".env", f"AWS_KEY={FAKE_AWS}\n"),
        ok("t4"),
        w("t5", "../outside.py", "x = 1\n"),
        ok("t5"),
        cc_assistant(
            [{"type": "tool_use", "id": "t6", "name": "Bash", "input": {"command": "pytest -q"}}], c, mid="t6"
        ),
        cc_tool_result("t6", "1 passed", c),
        cc_assistant([{"type": "text", "text": f"All done. (leaked {FAKE_AWS})"}], c, mid="a9"),
    ]
    write_transcript(claude_dir, root, "sess-1", lines)


def test_providers_overview(api):
    prov = {p["key"]: p for p in api.get(f"{V1}/providers").json()}
    assert set(prov) == {"claude-code", "continue", "github-copilot", "cursor", "manual"}
    assert prov["claude-code"]["capabilities"]["captures_file_edits"] is True
    assert prov["github-copilot"]["capabilities"]["captures_prompts"] is False
    assert prov["continue"]["capabilities"]["verified_against_real_install"] is False
    assert api.get(f"{V1}/providers/cursor/sessions").status_code == 422
    assert api.get(f"{V1}/providers/nope/sessions").status_code == 404


def test_import_claude_session(api, project, auth_project, claude_dir):
    apply_auth_feature(auth_project)  # the files the assistant wrote exist on disk
    (auth_project / ".env").write_text(f"AWS_KEY={FAKE_AWS}\n")
    make_transcript(claude_dir, auth_project)
    found = api.get(f"{V1}/providers/claude-code/sessions", params={"project_id": project}).json()
    assert [s["external_id"] for s in found] == ["sess-1"]
    r = api.post(f"{V1}/providers/claude-code/import", json={"project_id": project, "external_id": "sess-1"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["file_changes"] == 3  # auth.py, routes, test — .env and ../outside.py dropped
    (pat,) = body["patterns"]
    s = api.get(f"{V1}/sessions/{body['session_id']}").json()
    assert s["capture_source"] == "adapter" and s["provider"] == "claude-code" and s["outcome"] == "passed"
    assert s["meta"]["excluded_edits"] == 2 and s["meta"]["skipped"]["thinking_blocks"] == 1
    assert s["meta"]["observed_commands"] == [{"command": "pytest -q", "ok": True, "is_test": True}]
    assert FAKE_AWS not in json.dumps(s) and "SECRET-THOUGHT" not in json.dumps(s)
    p = api.get(f"{V1}/patterns/{pat['pattern_id']}").json()
    assert p["status"] == "observed"  # an observed passing test run is NOT verification
    assert p["provenance"]["observed_test_pass"] is True and p["category"] == "authentication"
    assert {f["path"] for f in p["content"]["files"]} == {
        "app/auth.py",
        "app/routes/auth_routes.py",
        "tests/test_auth.py",
    }
    # idempotent import; force re-imports
    again = api.post(f"{V1}/providers/claude-code/import", json={"project_id": project, "external_id": "sess-1"}).json()
    assert again["session_id"] == body["session_id"]
    forced = api.post(
        f"{V1}/providers/claude-code/import", json={"project_id": project, "external_id": "sess-1", "force": True}
    ).json()
    assert (
        forced["patterns"][0]["action"] == "duplicate"
    )  # same content → recorded as another occurrence, not a new pattern
    assert api.get(f"{V1}/sessions", params={"project_id": project}).json()["total"] == 1
    assert (
        api.post(
            f"{V1}/providers/claude-code/import", json={"project_id": project, "external_id": "missing"}
        ).status_code
        == 422
    )
    assert (
        api.post(f"{V1}/providers/cursor/import", json={"project_id": project, "external_id": "x"}).status_code == 422
    )


def test_import_respects_privacy_switches(api, project, auth_project, claude_dir):
    apply_auth_feature(auth_project)
    make_transcript(claude_dir, auth_project)
    api.patch(f"{V1}/config", json={"providers": {"claude-code": {"enabled": False}}})
    assert (
        api.post(
            f"{V1}/providers/claude-code/import", json={"project_id": project, "external_id": "sess-1"}
        ).status_code
        == 403
    )
    api.patch(
        f"{V1}/config", json={"providers": {"claude-code": {"enabled": True}}, "privacy": {"capture_enabled": False}}
    )
    assert (
        api.post(
            f"{V1}/providers/claude-code/import", json={"project_id": project, "external_id": "sess-1"}
        ).status_code
        == 403
    )
    assert api.post(f"{V1}/sessions", json={"project_id": project}).status_code == 403
    api.patch(f"{V1}/config", json={"privacy": {"capture_enabled": True}})
    api.patch(f"{V1}/projects/{project}", json={"capture_enabled": False})
    assert api.post(f"{V1}/sessions", json={"project_id": project}).status_code == 422  # per-project switch


# ------------------------------------------------------------------ lifecycle gate
@pytest.fixture()
def pattern_id(api, project):
    cand = extract_candidates(
        SessionInput(
            prompts=["Add JWT login"],
            files=[
                FileInput("app/auth.py", "created", AUTH_MODULE),
                FileInput("tests/test_auth.py", "created", AUTH_TEST),
            ],
            project_language="python",
        )
    )[0]
    return api.app.state.services.patterns.add_candidate(cand, project_id=project, session_id=None).pattern_id


def make_testing_session(svc, status="passed", results=(("t1", "passed"),)):
    with svc.session_factory() as db:
        ts = TestingSession(agent_key="unit-testing", status=status)
        db.add(ts)
        db.flush()
        for name, st in results:
            db.add(TestResult(testing_session_id=ts.id, agent_key="unit-testing", name=name, status=st))
        db.commit()
        return ts.id


def test_cannot_verify_without_evidence(api, pattern_id):
    svc = api.app.state.services
    with pytest.raises(LifecycleError, match="requires evidence"):
        svc.patterns.promote(pattern_id, "tested", None)  # type: ignore[arg-type]
    for sid, why in (
        (make_testing_session(svc, "failed", (("t", "failed"),)), "not 'passed'"),
        (make_testing_session(svc, "passed", ()), "no tests were executed"),
        (make_testing_session(svc, "passed", (("a", "skipped"),)), "no tests were executed"),
        (make_testing_session(svc, "passed", (("a", "passed"), ("b", "failed"))), "failed in that session"),
        (99999, "not found"),
    ):
        with pytest.raises(LifecycleError, match=why):
            svc.patterns.promote(pattern_id, "tested", sid)
    assert api.get(f"{V1}/patterns/{pattern_id}").json()["status"] == "observed"
    denied = [
        a for a in api.get(f"{V1}/audit", params={"action": "pattern.promote"}).json() if a["outcome"] == "denied"
    ]
    assert len(denied) >= 6


def test_tested_then_verified_requires_approval_or_reuse(api, pattern_id):
    svc = api.app.state.services
    good = make_testing_session(svc)
    assert svc.patterns.promote(pattern_id, "tested", good)["status"] == "tested"
    with pytest.raises(LifecycleError, match="approval or a successful reuse"):
        svc.patterns.promote(pattern_id, "verified", good)
    svc.patterns.approve(pattern_id)  # user approval flag recorded (status stays tested)
    assert api.get(f"{V1}/patterns/{pattern_id}").json()["status"] == "tested"
    v = svc.patterns.promote(pattern_id, "verified", good)
    assert v["status"] == "verified" and v["confidence"] >= 0.8 and v["verified_at"]
    # a direct jump from observed is also gated
    with pytest.raises(LifecycleError, match="only promotes"):
        svc.patterns.promote(pattern_id, "observed", good)  # demotion isn't a promotion path


def test_verified_via_successful_reuse(api, pattern_id):
    svc = api.app.state.services
    good = make_testing_session(svc)
    svc.patterns.promote(pattern_id, "tested", good)
    svc.patterns.record_use(pattern_id, success=True)
    assert svc.patterns.promote(pattern_id, "verified", good)["status"] == "verified"


def test_new_version_invalidates_verification(api, project, pattern_id):
    svc = api.app.state.services
    good = make_testing_session(svc)
    svc.patterns.approve(pattern_id)
    svc.patterns.promote(pattern_id, "tested", good)
    svc.patterns.promote(pattern_id, "verified", good)
    cand = extract_candidates(
        SessionInput(
            prompts=["Add JWT login"],
            files=[
                FileInput("app/auth.py", "created", AUTH_MODULE.replace("30", "45")),
                FileInput("tests/test_auth.py", "created", AUTH_TEST),
            ],
            project_language="python",
        )
    )[0]
    res = svc.patterns.add_candidate(cand, project_id=project, session_id=None)
    assert res.action == "new_version"
    p = api.get(f"{V1}/patterns/{pattern_id}").json()
    assert p["status"] == "observed" and p["verified_at"] is None and p["confidence"] < 0.5


def test_confidence_ordering_and_caps():
    order = [
        compute_confidence(s, 0, 0) for s in ("deprecated", "generated", "observed", "approved", "tested", "verified")
    ]
    assert order == sorted(order)
    assert compute_confidence("tested", 100, 0, 50) < 0.8  # glowing feedback can't fake verification
    assert compute_confidence("verified", 0, 100, -50) >= 0.8
    assert compute_confidence("observed", 5, 0) > compute_confidence("observed", 0, 5)
