"""Workspace baseline snapshots and change detection (git-independent, with optional git HEAD capture).

A baseline maps each readable, non-excluded file to ``raw_hash`` (sha256 of the raw bytes, used only to detect
change) and ``blob`` (hash of the *redacted* content stored in the blob store, used for diffs). Raw bytes are never
persisted; excluded files (env/keys/etc.) are never read into memory beyond the content sniff.
"""

from __future__ import annotations

import difflib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.learning.blobs import BlobStore, sha256_hex
from app.security.exclusions import ExclusionPolicy, walk_project
from app.security.secrets import redact

MAX_DIFF_CHARS = 200_000
DEFAULT_MAX_FILES = 20_000
DEFAULT_MAX_TOTAL_BYTES = 200_000_000


@dataclass(frozen=True)
class FileState:
    raw_hash: str
    blob: str | None  # redacted-content blob; None if too large for the budget
    size: int
    redactions: int = 0

    def to_json(self) -> list[object]:
        return [self.raw_hash, self.blob, self.size, self.redactions]

    @staticmethod
    def from_json(v: list[object]) -> FileState:
        return FileState(str(v[0]), None if v[1] is None else str(v[1]), int(v[2]), int(v[3]))  # type: ignore[call-overload]


@dataclass
class Baseline:
    files: dict[str, FileState]
    truncated: bool = False


@dataclass
class Change:
    path: str
    change_type: str  # created | modified | deleted | renamed
    old_path: str | None = None
    before_blob: str | None = None
    after_blob: str | None = None
    diff: str = ""
    lines_added: int = 0
    lines_removed: int = 0
    redactions: int = 0
    size: int = 0


def git_head(root: Path) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        out = subprocess.run(
            [git, "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def read_state(
    root: Path, rel: str, policy: ExclusionPolicy, blobs: BlobStore, store_blob: bool = True
) -> FileState | None:
    """Read one file defensively. Returns None for excluded/unreadable/binary files."""
    full = root / rel
    try:
        if full.is_symlink() or not full.is_file():
            return None
        data = full.read_bytes()
    except OSError:
        return None
    if policy.check_content(data).excluded:
        return None
    red = redact(data.decode("utf-8", errors="replace"))
    blob = blobs.put(red.text.encode("utf-8")) if store_blob else None
    return FileState(sha256_hex(data), blob, len(data), red.count)


def snapshot_project(
    root: Path,
    policy: ExclusionPolicy,
    blobs: BlobStore,
    max_files: int = DEFAULT_MAX_FILES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> Baseline:
    files: dict[str, FileState] = {}
    total = 0
    truncated = False
    for i, rel in enumerate(walk_project(root, policy)):
        if i >= max_files:
            truncated = True
            break
        store = total < max_total_bytes
        st = read_state(root, rel, policy, blobs, store_blob=store)
        if st is None:
            continue
        if not store:
            truncated = True
        total += st.size
        files[rel] = st
    return Baseline(files, truncated)


def _diff(before: str, after: str, path: str) -> tuple[str, int, int]:
    lines = list(
        difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True), f"a/{path}", f"b/{path}", n=3
        )
    )
    added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    text = "".join(ln if ln.endswith("\n") else ln + "\n" for ln in lines)
    if len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS] + "\n… [diff truncated]\n"
    return text, added, removed


def compute_changes(root: Path, baseline: Baseline, policy: ExclusionPolicy, blobs: BlobStore) -> list[Change]:
    """Compare the working tree with ``baseline``. Detects created/modified/deleted/renamed files."""
    current_paths = set(walk_project(root, policy))
    changes: list[Change] = []
    created: dict[str, FileState] = {}
    for rel in sorted(current_paths):
        st = read_state(root, rel, policy, blobs)
        if st is None:
            continue
        old = baseline.files.get(rel)
        if old is None:
            created[rel] = st
        elif old.raw_hash != st.raw_hash:
            before = blobs.get_text(old.blob) if old.blob else None
            after = blobs.get_text(st.blob) if st.blob else ""
            if before is None:
                diff, a, r = "(before-content unavailable: baseline size budget exceeded)\n", 0, 0
            else:
                diff, a, r = _diff(before, after or "", rel)
            changes.append(Change(rel, "modified", None, old.blob, st.blob, diff, a, r, st.redactions, st.size))
    deleted = {p: s for p, s in baseline.files.items() if p not in current_paths or not (root / p).exists()}
    # rename = same raw content at a different path
    by_hash = {s.raw_hash: p for p, s in deleted.items()}
    for rel, st in created.items():
        src = by_hash.get(st.raw_hash)
        if src and src in deleted:
            changes.append(Change(rel, "renamed", src, deleted[src].blob, st.blob, "", 0, 0, st.redactions, st.size))
            del deleted[src]
        else:
            after = blobs.get_text(st.blob) if st.blob else ""
            diff, a, r = _diff("", after or "", rel)
            changes.append(Change(rel, "created", None, None, st.blob, diff, a, r, st.redactions, st.size))
    for rel, old in deleted.items():
        before = blobs.get_text(old.blob) if old.blob else ""
        diff, a, r = _diff(before or "", "", rel)
        changes.append(Change(rel, "deleted", None, old.blob, None, diff, a, r, 0, old.size))
    return sorted(changes, key=lambda c: c.path)
