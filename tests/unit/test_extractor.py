"""Test plan: extraction/extractor.py
- an auth feature session yields ONE authentication pattern containing source, route and test files
- added dependencies come from manifest diffs only (not pre-existing ones)
- unrelated concerns in one session split into separate patterns; tiny noise folds into the dominant one
- no usable code → no pattern; deleted/excluded content ignored
- fingerprint is stable across renamed variables-in-content but changes with structure; content hash tracks content
- provenance/steps/verification hints are recorded; large files are truncated, flagged
"""

import pytest

from app.extraction.extractor import FileInput, SessionInput, extract_candidates
from tests.conftest import AUTH_MODULE, AUTH_ROUTES, AUTH_TEST

pytestmark = pytest.mark.unit


def auth_session(**kw):
    return SessionInput(
        prompts=["Add JWT login to the API. Users must authenticate with a password."],
        files=[
            FileInput("app/auth.py", "created", AUTH_MODULE),
            FileInput("app/routes/auth_routes.py", "created", AUTH_ROUTES),
            FileInput("tests/test_auth.py", "created", AUTH_TEST),
            FileInput("requirements.txt", "modified", "fastapi\nbcrypt>=4\nPyJWT>=2.8\n", before="fastapi\n"),
        ],
        commands=[{"command": "pytest -q", "ok": True, "is_test": True}],
        project_language="python",
        project_frameworks=[
            {"name": "FastAPI", "version": "0.110.0", "ecosystem": "pypi", "dev": False},
            {"name": "pytest", "version": "", "ecosystem": "pypi", "dev": True},
        ],
        provider="claude-code",
        session_id=7,
        **kw,
    )


def test_auth_session_becomes_one_pattern():
    (c,) = extract_candidates(auth_session())
    assert c.category == "authentication" and c.language == "python"
    assert (c.framework, c.framework_version) == ("FastAPI", "0.110.0")
    paths = [f["path"] for f in c.content["files"]]
    assert {"app/auth.py", "app/routes/auth_routes.py", "tests/test_auth.py"} <= set(paths)
    assert c.content["tests"] == ["tests/test_auth.py"]
    assert c.title.startswith("Authentication:") and "JWT login" in c.title
    assert {d.name for d in c.dependencies} == {"bcrypt", "pyjwt"}  # fastapi pre-existed
    assert any(s.startswith("Add pypi dependency bcrypt") for s in c.content["steps"])
    assert c.provenance["observed_test_pass"] is True and c.provenance["session_id"] == 7
    syms = {s["name"] for f in c.content["files"] for s in f.get("symbols", [])}
    assert {"hash_password", "verify_password", "create_access_token", "login"} <= syms
    assert {"method": "POST", "path": "/login"} in c.content["files"][1]["routes"] or any(
        {"method": "POST", "path": "/login"} in f.get("routes", []) for f in c.content["files"]
    )
    assert c.category_evidence


def test_no_usable_code_yields_nothing():
    s = SessionInput(
        prompts=["x"],
        files=[
            FileInput("README.md", "modified", "# hi\n"),
            FileInput("a.py", "deleted", None),
            FileInput("tiny.py", "created", "x=1\n"),
        ],
    )
    assert extract_candidates(s) == []


def test_unrelated_concerns_split_into_patterns():
    users_migration = "from alembic import op\nimport sqlalchemy as sa\n\ndef upgrade():\n    op.create_table('users', sa.Column('id', sa.Integer))\n    op.create_table('orders', sa.Column('id', sa.Integer))\n\ndef downgrade():\n    op.drop_table('users')\n"
    s = SessionInput(
        prompts=["Add login and a users table", "Also add a settings migration"],
        files=[
            FileInput("app/auth.py", "created", AUTH_MODULE),
            FileInput("migrations/0001_users.py", "created", users_migration),
        ],
        project_language="python",
    )
    cats = {c.category for c in extract_candidates(s)}
    assert {"authentication", "database-schema"} <= cats


def test_fingerprint_vs_content_hash():
    a = extract_candidates(auth_session())[0]
    same = extract_candidates(auth_session())[0]
    assert (a.fingerprint, a.content_hash) == (same.fingerprint, same.content_hash)  # deterministic
    tweaked = auth_session()
    tweaked.files[0] = FileInput(
        "app/auth.py", "created", AUTH_MODULE.replace("minutes: int = 30", "minutes: int = 60")
    )
    t = extract_candidates(tweaked)[0]
    assert t.fingerprint == a.fingerprint and t.content_hash != a.content_hash  # same structure, new content
    restructured = auth_session()
    restructured.files[0] = FileInput(
        "app/auth.py", "created", AUTH_MODULE + "\n\ndef refresh_token():\n    return 1\n"
    )
    assert extract_candidates(restructured)[0].fingerprint != a.fingerprint


def test_large_files_truncated_and_flagged():
    big = AUTH_MODULE + "\n" + "# filler\n" * 20_000
    s = auth_session()
    s.files[0] = FileInput("app/auth.py", "created", big)
    (c,) = extract_candidates(s)
    f = next(x for x in c.content["files"] if x["path"] == "app/auth.py")
    assert f["truncated"] is True and len(f["content"]) <= 60_000


def test_failed_tests_recorded_as_not_passed():
    s = auth_session()
    s.commands = [{"command": "pytest -q", "ok": False, "is_test": True}]
    (c,) = extract_candidates(s)
    assert c.provenance["observed_test_pass"] is False


def test_a_pasted_prompt_wrapper_tag_is_never_the_title():
    """The author's first imported session started with `<pasted_content id="5493">`, giving five patterns titled
    "Testing: <pasted_content id="5493">"."""
    from app.extraction.extractor import _title_from_prompt

    wrapped = '<pasted_content id="5493">\nAdd JWT login with password hashing.\n</pasted_content>'
    assert _title_from_prompt(wrapped, "testing") == "Testing: Add JWT login with password hashing"
    assert (
        _title_from_prompt('<pasted_content id="1">\n</pasted_content>', "testing") == "Testing"
    )  # nothing but wrappers
    assert (
        _title_from_prompt("Fix the <div> layout on mobile", "ui-component")
        == "UI component: Fix the <div> layout on mobile"
    )
    assert _title_from_prompt("Add retries.", "error-handling").startswith("Error handling: Add retries")
