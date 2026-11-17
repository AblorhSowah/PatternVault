"""Test plan: security/exclusions.py
- credential/key/env files are always excluded; .env.example is allowed
- excluded directories, low-value/binary files and user rules are excluded
- content checks: size, binary, private key material
- walk_project honours exclusions, .gitignore and does not follow symlinks
"""

import os
from pathlib import Path

import pytest

from app.security.exclusions import ExclusionPolicy, walk_project

pytestmark = pytest.mark.unit
P = ExclusionPolicy()


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        ".env.production",
        "config/.env",
        "prod.env",
        "certs/server.pem",
        "keys/id_rsa",
        "id_ed25519.pub",
        "deploy/credentials.json",
        "gcp/service-account-prod.json",
        ".npmrc",
        "infra/prod.tfvars",
        "terraform.tfstate",
        "secrets.yaml",
        "app/.aws/credentials",
    ],
)
def test_sensitive_paths_excluded(path):
    assert P.check_path(path).excluded, path


@pytest.mark.parametrize(
    "path", [".env.example", ".env.sample", ".env.template", "src/app.py", "README.md", "a/b/credentials_helper.py"]
)
def test_normal_or_template_paths_allowed(path):
    assert not P.check_path(path).excluded, path


@pytest.mark.parametrize(
    "path",
    [
        "node_modules/x/index.js",
        "a/.venv/lib/x.py",
        ".git/config",
        "dist/app.js",
        "__pycache__/m.pyc",
        "build/out.o",
        "package-lock.json",
        "assets/logo.png",
        "app.min.js",
        "data.sqlite",
        "x/uv.lock",
    ],
)
def test_generated_and_binary_excluded(path):
    assert P.check_path(path).excluded, path


def test_user_rules():
    pol = ExclusionPolicy(extra_globs=["legacy/**", "*.draft.md"])
    assert pol.check_path("legacy/old/x.py").category == "user_rule"
    assert pol.check_path("docs/a.draft.md").excluded
    assert not pol.check_path("src/x.py").excluded


def test_content_checks():
    pol = ExclusionPolicy(max_file_bytes=100)
    assert pol.check_content(b"x" * 101).category == "size"
    assert pol.check_content(b"ab\x00cd").category == "binary"
    assert ExclusionPolicy().check_content(b"-----BEGIN RSA PRIVATE KEY-----\nabc").category == "sensitive"
    assert not ExclusionPolicy().check_content(b"print('hi')\n").excluded


def test_walk_project(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.py").write_text("x=1")
    (tmp_path / ".env").write_text("SECRET=1")
    (tmp_path / "node_modules/p").mkdir(parents=True)
    (tmp_path / "node_modules/p/i.js").write_text("1")
    (tmp_path / "ignored.txt").write_text("1")
    (tmp_path / ".gitignore").write_text("ignored.txt\n")
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("nope")
    os.symlink(outside, tmp_path / "src/link.txt")
    files = walk_project(tmp_path, ExclusionPolicy())
    assert "src/a.py" in files
    assert ".gitignore" in files
    assert ".env" not in files and "ignored.txt" not in files
    assert not any(f.startswith("node_modules") for f in files)
    assert "src/link.txt" not in files
