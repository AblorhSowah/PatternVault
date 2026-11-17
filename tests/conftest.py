"""Shared fixtures. Everything uses a throw-away PATTERNVAULT_HOME under pytest's tmp_path."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import RuntimeSettings
from app.main import create_app
from app.services.container import Services

TOKEN = "test-token-0123456789abcdef0123456789abcdef"


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    return tmp_path / "pv-home"


@pytest.fixture()
def services(home: Path) -> Iterator[Services]:
    svc = Services(RuntimeSettings(home=home))
    yield svc
    svc.close()


@pytest.fixture()
def client(services: Services) -> Iterator[TestClient]:
    app = create_app(services=services, token=TOKEN)
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield c


@pytest.fixture()
def anon_client(services: Services) -> Iterator[TestClient]:
    app = create_app(services=services, token=TOKEN)
    with TestClient(app) as c:
        yield c


def run_git(cwd: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "HOME": str(cwd),
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True, env=env)
    return out.stdout


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("# demo\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "init")
    return repo


# --------------------------------------------------------------------------- Phase 2 fixtures
import json  # noqa: E402

from app.learning.adapters.claude_code import encode_project_dir  # noqa: E402
from app.learning.adapters.registry import AdapterRegistry  # noqa: E402

FAKE_AWS = "AKIA" + "IOSFODNN7EXAMPLE"


@pytest.fixture()
def claude_dir(tmp_path: Path) -> Path:
    d = tmp_path / "claude-config"
    (d / "projects").mkdir(parents=True)
    return d


@pytest.fixture()
def continue_dir(tmp_path: Path) -> Path:
    d = tmp_path / "continue-dir"
    (d / "sessions").mkdir(parents=True)
    return d


@pytest.fixture()
def services_with_adapters(home: Path, claude_dir: Path, continue_dir: Path) -> Iterator[Services]:
    svc = Services(RuntimeSettings(home=home), adapters=AdapterRegistry(claude_dir, continue_dir))
    yield svc
    svc.close()


@pytest.fixture()
def api(services_with_adapters: Services) -> Iterator[TestClient]:
    app = create_app(services=services_with_adapters, token=TOKEN)
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield c


def write_transcript(claude_dir: Path, project_root: Path, session_id: str, lines: list[dict]) -> Path:
    """Write a JSONL transcript in Claude Code's on-disk layout/schema (as observed in a real session)."""
    d = claude_dir / "projects" / encode_project_dir(project_root.resolve())
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{session_id}.jsonl"
    for x in lines:  # real transcripts carry the session id (== file stem) on every message line
        if "sessionId" in x:
            x["sessionId"] = session_id
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return f


def cc_user(text, cwd, sid="s1", ts="2026-01-01T10:00:00.000Z", **extra):
    return {
        "type": "user",
        "sessionId": sid,
        "cwd": cwd,
        "timestamp": ts,
        "isSidechain": False,
        "message": {"role": "user", "content": text},
        **extra,
    }


def cc_assistant(blocks, cwd, sid="s1", mid="m1", ts="2026-01-01T10:00:05.000Z", sidechain=False):
    return {
        "type": "assistant",
        "sessionId": sid,
        "cwd": cwd,
        "timestamp": ts,
        "isSidechain": sidechain,
        "message": {"id": mid, "role": "assistant", "model": "claude-test", "content": blocks},
    }


def cc_tool_result(tool_id, text, cwd, is_error=False, sid="s1"):
    return {
        "type": "user",
        "sessionId": sid,
        "cwd": cwd,
        "timestamp": "2026-01-01T10:00:09.000Z",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": text, "is_error": is_error}],
        },
    }


AUTH_MODULE = '''"""JWT authentication helpers."""
import datetime

import bcrypt
import jwt

SECRET = "change-me"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def create_access_token(user_id: int, minutes: int = 30) -> str:
    exp = datetime.datetime.utcnow() + datetime.timedelta(minutes=minutes)
    return jwt.encode({"sub": str(user_id), "exp": exp}, SECRET, algorithm="HS256")
'''

AUTH_ROUTES = """from fastapi import APIRouter, HTTPException

from app.auth import create_access_token, verify_password

router = APIRouter()


@router.post("/login")
def login(username: str, password: str):
    user = lookup_user(username)
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid credentials")
    return {"access_token": create_access_token(user.id)}
"""

AUTH_TEST = """from app.auth import hash_password, verify_password


def test_password_roundtrip():
    h = hash_password("pw12345")
    assert verify_password("pw12345", h)
    assert not verify_password("nope", h)
"""


@pytest.fixture()
def auth_project(tmp_path: Path) -> Path:
    """A small FastAPI project (git-initialised) to run capture against."""
    root = tmp_path / "shop-api"
    (root / "app").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "requirements.txt").write_text("fastapi==0.110.0\nuvicorn\n")
    (root / "app/__init__.py").write_text("")
    (root / "app/main.py").write_text(
        "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n@app.get('/health')\ndef health():\n    return {'ok': True}\n"
    )
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "init")
    return root


def apply_auth_feature(root: Path) -> None:
    (root / "app").mkdir(exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "app/auth.py").write_text(AUTH_MODULE)
    (root / "app/routes").mkdir(exist_ok=True)
    (root / "app/routes/auth_routes.py").write_text(AUTH_ROUTES)
    (root / "tests/test_auth.py").write_text(AUTH_TEST)
    (root / "requirements.txt").write_text("fastapi==0.110.0\nuvicorn\nbcrypt>=4.0\nPyJWT>=2.8\n")


# --------------------------------------------------------------------------- Phase 3 fixtures
from app.extraction.extractor import FileInput, SessionInput, extract_candidates  # noqa: E402

FLASK_LOGIN = """from flask import Blueprint, redirect, request, session
from flask_login import login_user, logout_user, login_required

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["POST"])
def login():
    user = find_user(request.form["username"])
    if user and user.check_password(request.form["password"]):
        login_user(user)
        session["user_id"] = user.id
        return redirect("/")
    return "invalid", 401


@bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect("/login")
"""

ALEMBIC_USERS = '''"""create users and orders tables"""
from alembic import op
import sqlalchemy as sa


def upgrade():
    op.create_table("users", sa.Column("id", sa.Integer, primary_key=True), sa.Column("email", sa.String(255), nullable=False))
    op.create_table("orders", sa.Column("id", sa.Integer, primary_key=True), sa.Column("user_id", sa.ForeignKey("users.id")))


def downgrade():
    op.drop_table("orders")
    op.drop_table("users")
'''

LARAVEL_CONTROLLER = """<?php
namespace App\\Http\\Controllers;

use App\\Models\\Warehouse;
use Illuminate\\Http\\Request;

class WarehouseController extends Controller
{
    public function index() { return Warehouse::all(); }

    public function store(Request $request)
    {
        $data = $request->validate(['name' => 'required|string', 'location' => 'required']);
        return Warehouse::create($data);
    }
}
"""


def make_candidate(kind: str, *, fw=None, fw_ver=None, language="python", tweak: str = ""):
    """Build realistic pattern candidates through the real extractor."""
    fw_list = (
        [
            {
                "name": fw,
                "version": fw_ver or "",
                "ecosystem": "pypi" if language == "python" else "composer",
                "dev": False,
            }
        ]
        if fw
        else []
    )
    if kind == "jwt":
        files = [
            FileInput("app/auth.py", "created", AUTH_MODULE + tweak),
            FileInput("app/routes/auth_routes.py", "created", AUTH_ROUTES),
            FileInput("tests/test_auth.py", "created", AUTH_TEST),
            FileInput("requirements.txt", "modified", "bcrypt\nPyJWT\n", before=""),
        ]
        prompt = "Add JWT login with password hashing"
    elif kind == "session":
        files = [
            FileInput("app/auth/views.py", "created", FLASK_LOGIN),
            FileInput("requirements.txt", "modified", "flask-login\n", before=""),
        ]
        prompt = "Add session based login with flask-login"
    elif kind == "migration":
        files = [FileInput("migrations/versions/0001_users.py", "created", ALEMBIC_USERS)]
        prompt = "Create the users and orders database tables migration"
    elif kind == "laravel":
        files = [FileInput("app/Http/Controllers/WarehouseController.php", "created", LARAVEL_CONTROLLER)]
        prompt = "Inventory management: warehouse controller with validation"
        language = "php"
    else:
        raise ValueError(kind)
    out = extract_candidates(
        SessionInput(prompts=[prompt], files=files, project_language=language, project_frameworks=fw_list)
    )
    assert out, kind
    return out[0]


@pytest.fixture()
def kb(services: Services):
    """Services with a small knowledge base; returns (services, {name: pattern_id})."""
    ids = {}
    for name, kw in (
        ("jwt", {"fw": "FastAPI", "fw_ver": "0.110.0"}),
        ("session", {"fw": "Flask", "fw_ver": "3.0"}),
        ("migration", {"fw": "SQLAlchemy", "fw_ver": "2.0"}),
        ("laravel", {"fw": "Laravel", "fw_ver": "9.0", "language": "php"}),
    ):
        ids[name] = services.patterns.add_candidate(
            make_candidate(name, **kw), project_id=None, session_id=None
        ).pattern_id
    return services, ids


def make_testing_session_for(svc, status="passed", results=(("t1", "passed"),)):
    from app.models import TestingSession, TestResult

    with svc.session_factory() as db:
        ts = TestingSession(agent_key="unit-testing", status=status)
        db.add(ts)
        db.flush()
        for name, st in results:
            db.add(TestResult(testing_session_id=ts.id, agent_key="unit-testing", name=name, status=st))
        db.commit()
        return ts.id
