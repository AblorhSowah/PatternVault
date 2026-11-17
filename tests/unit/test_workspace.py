"""Test plan: learning/workspace.py
- baseline covers normal files only: .env, keys and binaries are neither read into the blob store nor tracked
- created / modified / deleted / renamed detection; unified diffs with counts
- secrets are redacted in stored blobs while raw change detection still notices a secret-line edit
- size budget: over-budget files are tracked by hash without blobs and flagged
"""

import os

import pytest

from app.learning.blobs import BlobStore
from app.learning.workspace import compute_changes, git_head, snapshot_project
from app.security.exclusions import ExclusionPolicy
from tests.conftest import FAKE_AWS

pytestmark = pytest.mark.unit


@pytest.fixture()
def env(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    return root, BlobStore(tmp_path / "blobs"), ExclusionPolicy()


def blobs_content(bs: BlobStore) -> str:
    return "".join(p.read_text(errors="ignore") for p in bs.root.rglob("*") if p.is_file())


def test_baseline_skips_sensitive_and_binary(env):
    root, bs, pol = env
    (root / "a.py").write_text("x = 1\n")
    (root / ".env").write_text("API_KEY=supersecretvalue123\n")
    (root / "key.pem").write_text("-----BEGIN RSA PRIVATE KEY-----\nabc\n")
    (root / "blob.dat").write_bytes(b"\x00\x01\x02")
    (root / "notes.txt").write_text(
        "-----BEGIN OPENSSH PRIVATE KEY-----\nzzz\n"
    )  # key material in an innocent filename
    base = snapshot_project(root, pol, bs)
    assert "a.py" in base.files and ".env" not in base.files and "key.pem" not in base.files
    assert "notes.txt" not in base.files  # content sniff caught the key
    assert "supersecretvalue123" not in blobs_content(bs) and "PRIVATE KEY" not in blobs_content(bs)


def test_change_detection(env):
    root, bs, pol = env
    (root / "keep.py").write_text("a\nb\nc\n")
    (root / "gone.py").write_text("old\n")
    (root / "move_me.py").write_text("same content\n")
    base = snapshot_project(root, pol, bs)
    (root / "keep.py").write_text("a\nB\nc\nd\n")
    (root / "gone.py").unlink()
    os.rename(root / "move_me.py", root / "moved.py")
    (root / "new.py").write_text("hello\nworld\n")
    changes = {c.path: c for c in compute_changes(root, base, pol, bs)}
    assert (
        changes["keep.py"].change_type == "modified"
        and changes["keep.py"].lines_added == 2
        and changes["keep.py"].lines_removed == 1
    )
    assert "-b\n" in changes["keep.py"].diff and "+B\n" in changes["keep.py"].diff
    assert changes["gone.py"].change_type == "deleted" and changes["gone.py"].lines_removed == 1
    assert changes["moved.py"].change_type == "renamed" and changes["moved.py"].old_path == "move_me.py"
    assert changes["new.py"].change_type == "created" and changes["new.py"].lines_added == 2
    assert "move_me.py" not in changes


def test_unchanged_tree_yields_no_changes(env):
    root, bs, pol = env
    (root / "a.py").write_text("x\n")
    base = snapshot_project(root, pol, bs)
    assert compute_changes(root, base, pol, bs) == []


def test_secret_edit_detected_but_never_stored_raw(env):
    root, bs, pol = env
    (root / "cfg.py").write_text(f'KEY = "{FAKE_AWS}"\nA = 1\n')
    base = snapshot_project(root, pol, bs)
    (root / "cfg.py").write_text('KEY = "AKIAZZZZZZZZZZZZZZZZ"\nA = 1\n')  # rotate the secret
    changes = compute_changes(root, base, pol, bs)
    assert len(changes) == 1 and changes[0].change_type == "modified"  # raw hash differs → change noticed
    assert FAKE_AWS not in blobs_content(bs) and "AKIAZZZZZZZZZZZZZZZZ" not in blobs_content(bs)
    assert changes[0].redactions == 1


def test_secret_added_by_new_file_is_redacted_in_diff(env):
    root, bs, pol = env
    base = snapshot_project(root, pol, bs)
    (root / "new.py").write_text(f'k = "{FAKE_AWS}"\n')
    (c,) = compute_changes(root, base, pol, bs)
    assert FAKE_AWS not in c.diff and "[REDACTED:aws-access-key-id]" in c.diff


def test_budget_truncation_flagged(env):
    root, bs, pol = env
    for i in range(5):
        (root / f"f{i}.py").write_text("x = 1\n" * 100)
    base = snapshot_project(root, pol, bs, max_files=3)
    assert base.truncated and len(base.files) == 3
    base2 = snapshot_project(root, pol, bs, max_total_bytes=1)
    assert base2.truncated and any(s.blob is None for s in base2.files.values())


def test_git_head(git_repo, tmp_path):
    assert len(git_head(git_repo) or "") == 40
    assert git_head(tmp_path) is None
