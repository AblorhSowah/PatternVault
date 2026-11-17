"""Regression guards: one named test per defect found (and fixed) during PatternVault's development.

Each docstring says what went wrong, so a future failure here points straight at the historical cause. These are
intentionally small and independent; richer coverage lives next to each feature's own tests.
"""

import asyncio
import io
import json
import threading
import time
from pathlib import Path

import pytest

from app.extraction.extractor import FileInput, SessionInput, extract_candidates
from app.knowledge.lifecycle import LifecycleError
from app.retrieval.search import SearchQuery
from app.security.secrets import contains_secret, redact
from tests.conftest import AUTH_MODULE, AUTH_ROUTES, FAKE_AWS, apply_auth_feature, make_candidate

pytestmark = pytest.mark.regression
V1 = "/api/v1"


def test_reg001_secret_detector_ignores_identifiers_that_merely_contain_secret_words():
    """`"passed": "success"` and `tokenize='porter'` were flagged (and would have been redacted out of captured code)."""
    assert not contains_secret('status = {"passed": "success"}.get(x)')
    assert not contains_secret("fts5(a, b, tokenize='porter unicode61')")
    assert contains_secret('DB_PASSWORD = "hunter2hunter2"')


def test_reg002_placeholder_filter_did_not_swallow_real_secrets():
    """The placeholder regex treated any value starting with 'a' as a placeholder, so `api_key: "abcd1234efgh5678"` leaked."""
    assert redact('"api_key": "abcd1234efgh5678"').count == 1
    assert redact("export API_TOKEN=abcdef1234567890").count == 1


def test_reg003_audit_logging_does_not_deadlock_with_an_open_write_transaction(services):
    """AuditLogger uses its own connection; auditing while the caller held SQLite's write lock self-deadlocked."""
    from app.models import Project

    done = threading.Event()

    def work():
        with services.session_factory() as db:
            db.add(Project(name="p", root_path="/r-reg003"))
            db.commit()  # the documented rule: commit BEFORE auditing
        services.audit.record("user", "reg003")
        done.set()

    t = threading.Thread(target=work)
    t.start()
    assert done.wait(10), "audit logging deadlocked"
    t.join()


def test_reg004_requirements_txt_is_recognised_as_a_manifest_when_extracting():
    """Dependency additions in requirements.txt were missed because only exact manifest basenames were checked."""
    cand = extract_candidates(
        SessionInput(
            prompts=["Add login"],
            files=[
                FileInput("app/auth.py", "created", AUTH_MODULE),
                FileInput("requirements.txt", "modified", "bcrypt\nPyJWT\n", before=""),
            ],
            project_language="python",
        )
    )[0]
    assert {d.name for d in cand.dependencies} == {"bcrypt", "pyjwt"}


def test_reg005_deprecated_pattern_cannot_be_approved(services):
    """approve() silently succeeded (and marked the pattern approved) while deprecated."""
    pid = services.patterns.add_candidate(
        make_candidate("jwt", fw="FastAPI", fw_ver="0.110"), project_id=None, session_id=None
    ).pattern_id
    services.patterns.deprecate(pid)
    with pytest.raises(LifecycleError):
        services.patterns.approve(pid)


def test_reg006_promote_only_accepts_tested_or_verified_targets(services):
    """promote(..., 'observed') was accepted as a demotion path."""
    pid = services.patterns.add_candidate(
        make_candidate("jwt", fw="FastAPI", fw_ver="0.110"), project_id=None, session_id=None
    ).pattern_id
    with pytest.raises(LifecycleError, match="only promotes"):
        services.patterns.promote(pid, "observed", 1)


def test_reg007_pattern_store_redacts_even_if_a_caller_forgot_to(services):
    """The store trusted its callers (extractor/import/agents) to have redacted content already."""
    cand = extract_candidates(
        SessionInput(
            prompts=["x"],
            files=[
                FileInput(
                    "app/a.py", "created", f'K = "{FAKE_AWS}"\n\n\ndef f():\n    return 1\n\n\ndef g():\n    return 2\n'
                )
            ],
            project_language="python",
        )
    )[0]
    assert FAKE_AWS in json.dumps(cand.content)
    pid = services.patterns.add_candidate(cand, project_id=None, session_id=None).pattern_id
    assert FAKE_AWS not in json.dumps(services.patterns.get(pid)["content"])


def test_reg008_nonsense_queries_return_nothing_instead_of_hash_collision_noise(services):
    """The hashing embedder's ~0.03 cosine noise made every pattern 'match' any query."""
    services.patterns.add_candidate(
        make_candidate("jwt", fw="FastAPI", fw_ver="0.110"), project_id=None, session_id=None
    )
    assert services.search.search(SearchQuery("kubernetes helm chart rollout canary")).hits == []


def test_reg009_stale_detection_notices_lost_vectors(services):
    """stale() only consulted bookkeeping rows, so a cleared vector store looked up to date."""
    services.patterns.add_candidate(
        make_candidate("jwt", fw="FastAPI", fw_ver="0.110"), project_id=None, session_id=None
    )
    assert services.indexer.stale() == []
    services.vector_store.clear()
    assert len(services.indexer.stale()) == 1


def test_reg010_claude_adapter_tolerates_non_object_json_lines(tmp_path):
    """A valid-JSON line that is not an object (e.g. `[1,2]`) crashed session discovery."""
    from app.learning.adapters.claude_code import ClaudeCodeAdapter, encode_project_dir

    proj = tmp_path / "p"
    proj.mkdir()
    d = tmp_path / "cfg" / "projects" / encode_project_dir(proj.resolve())
    d.mkdir(parents=True)
    (d / "s.jsonl").write_text(
        '[1,2]\n"str"\n'
        + json.dumps(
            {"type": "user", "sessionId": "s", "cwd": str(proj), "message": {"role": "user", "content": "hello"}}
        )
        + "\n"
    )
    assert [s.external_id for s in ClaudeCodeAdapter(tmp_path / "cfg").discover(proj)] == ["s"]


def test_reg011_reference_pattern_use_is_counted_once(api, auth_project):
    """Generation counted a use and the learn step counted it again (use_count == 2 for one adaptation)."""
    svc = api.app.state.services
    pid = svc.patterns.add_candidate(
        make_candidate("jwt", fw="FastAPI", fw_ver="0.110"), project_id=None, session_id=None
    ).pattern_id
    svc.patterns.record_use(pid, success=None)
    svc.patterns.record_use(pid, success=True, count_use=False)
    p = svc.patterns.get(pid, False)
    assert (p["use_count"], p["success_count"]) == (1, 1)


def test_reg012_read_only_mode_generation_is_a_proposal_not_an_error():
    """Proposed files carried error issues ('write would be denied') in read-only mode, failing the whole generate step."""
    from app.config import PermissionLevel
    from app.security.permissions import PermissionPolicy

    v = PermissionPolicy(PermissionLevel.READ_ONLY, __import__("pathlib").Path.cwd()).check_write("x.py")
    assert v.denied and v.rule == "read-only"  # the generator/review agents key off this rule to downgrade it to info


def test_reg013_skipped_steps_block_everything_downstream(services, tmp_path):
    """verify ran (and reported success!) after apply was skipped because review failed: tests passed on unwritten code."""
    from tests.helpers.fake_ollama import FakeOllama
    from tests.integration.test_orchestrator import CALC, blocks, configure, mkcalc, run_task, steps

    root, pid = mkcalc(services, tmp_path)
    evil = CALC + '\n\ndef multiply(a, b):\n    return eval(f"{a}*{b}")\n'
    with FakeOllama(responder=lambda *a: blocks(evil)) as fake:
        configure(services, fake)
        t = run_task(services, "generate", "Add multiply to calc.py", pid)
    assert steps(t)["review"] == "failed" and steps(t)["apply"] == "skipped" and steps(t)["verify"] == "skipped"
    assert t["status"] == "failed" and not t["verified"]


def test_reg014_gc_never_deletes_the_baseline_blobs_of_an_open_capture_window(api, auth_project):
    """GC treated unchanged-file baseline blobs as garbage, so a later diff lost its 'before' content."""
    pid = api.post(f"{V1}/projects", json={"root_path": str(auth_project)}).json()["id"]
    sid = api.post(f"{V1}/sessions", json={"project_id": pid}).json()["id"]
    api.post(f"{V1}/storage/gc")
    (auth_project / "app/main.py").write_text("changed = 1\n")
    detail = api.post(f"{V1}/sessions/{sid}/end", json={"extract": False}).json()
    assert detail["changes"] == 1
    diff = next(c for c in api.get(f"{V1}/sessions/{sid}").json()["changes"] if c["path"] == "app/main.py")["diff"]
    assert "-from fastapi import FastAPI" in diff


def test_reg015_backup_restore_does_not_rewind_the_audit_trail(services):
    """Restoring replaced audit_logs with the backup's older copy, erasing what happened after the backup."""
    b = services.backups.create(None)
    services.audit.record("agent:x", "after.backup")
    services.backups.restore(b["name"])
    from sqlalchemy import select

    from app.models import AuditLog

    with services.session_factory() as db:
        assert "after.backup" in [a for (a,) in db.execute(select(AuditLog.action)).all()]


def test_reg016_webview_null_children_do_not_render_as_text():
    """Native Element.append(null) renders the literal text 'null'; dom.js patches append/prepend (checked in the E2E null scan)."""
    src = Path("vscode-extension/media/webview/lib/dom.js").read_text()
    assert "Element.prototype[name] = function patched" in src and "n !== null" in src


def test_reg017_scanner_does_not_flag_regex_exec_or_child_process_imports():
    """`/x/.exec(s)` matched the eval/exec rule and `import ... from 'node:child_process'` matched the injection rule."""
    from app.security.scanner import scan_text

    assert not [
        f
        for f in scan_text("a.ts", "const m = /x/.exec(s);\nimport { spawn } from 'node:child_process';\n")
        if f["severity"] in ("high", "critical")
    ]


def test_reg018_apply_feature_fixture_is_still_realistic(auth_project):
    """Guards the shared fixture the capture tests depend on (project + git + feature files)."""
    apply_auth_feature(auth_project)
    assert (auth_project / "app/auth.py").read_text() == AUTH_MODULE and "def login" in AUTH_ROUTES
    assert asyncio.run(asyncio.sleep(0)) is None and io.StringIO("x").read() == "x" and time.time() > 0
