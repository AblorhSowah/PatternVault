"""Session capture: workspace capture windows, manual entries and adapter imports.

Everything stored passes through secret redaction and the exclusion policy first; captured text is scanned for
prompt-injection patterns and flagged (never executed).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.extraction.language import detect_language
from app.learning.adapters.base import CapturedSession, ExternalSession, FileEdit
from app.learning.adapters.registry import AdapterRegistry
from app.learning.blobs import BlobStore, sha256_hex
from app.learning.workspace import Baseline, FileState, compute_changes, git_head, snapshot_project
from app.models import (
    AIProvider,
    CapturedResponse,
    DevSession,
    FileChange,
    FileSnapshot,
    Project,
    UserPrompt,
)
from app.models.base import utcnow
from app.security.exclusions import ExclusionPolicy
from app.security.secrets import redact
from app.security.untrusted import scan_untrusted

MAX_TEXT_CHARS = 200_000


class CaptureError(ValueError):
    pass


def _clip(text: str) -> str:
    return text if len(text) <= MAX_TEXT_CHARS else text[:MAX_TEXT_CHARS] + "\n… [truncated]"


class CaptureService:
    def __init__(
        self,
        blobs: BlobStore,
        adapters: AdapterRegistry,
        audit: Any,
        events: Any,
        policy_factory: Any,
        storage_guard: Any = None,
    ) -> None:
        self.blobs = blobs
        self.adapters = adapters
        self.audit = audit
        self.events = events
        self._policy_factory = policy_factory  # () -> ExclusionPolicy (reads current user config)
        self._storage_guard = storage_guard  # () -> None, raises StorageLimitExceeded

    # ----------------------------------------------------------------- helpers
    def _policy(self) -> ExclusionPolicy:
        policy: ExclusionPolicy = self._policy_factory()
        return policy

    def ensure_provider(self, db: Session, key: str) -> AIProvider:
        adapter = self.adapters.get(key)
        row = db.execute(select(AIProvider).where(AIProvider.key == key)).scalar_one_or_none()
        if row is None:
            row = AIProvider(
                key=key,
                display_name=adapter.display_name,
                kind=adapter.kind,
                capabilities=adapter.capabilities.to_dict(),
            )
            db.add(row)
            db.flush()
        else:
            row.capabilities = adapter.capabilities.to_dict()
        return row

    def _project(self, db: Session, project_id: int) -> Project:
        if self._storage_guard:
            self._storage_guard()
        project = db.get(Project, project_id)
        if project is None:
            raise CaptureError("project not found")
        if not project.capture_enabled:
            raise CaptureError("capture is disabled for this project")
        return project

    # ----------------------------------------------------------------- workspace capture window
    def start_workspace_session(
        self, db: Session, project_id: int, title: str = "", provider_key: str = "manual"
    ) -> DevSession:
        project = self._project(db, project_id)
        root = Path(project.root_path)
        if not root.is_dir():
            raise CaptureError("project root no longer exists")
        provider = self.ensure_provider(db, provider_key)
        baseline = snapshot_project(root, self._policy(), self.blobs)
        manifest = json.dumps(
            {"files": {p: s.to_json() for p, s in baseline.files.items()}, "truncated": baseline.truncated}
        )
        session = DevSession(
            project_id=project.id,
            provider_id=provider.id,
            title=title or "Capture window",
            status="active",
            capture_source="workspace",
            git_head_start=git_head(root),
            meta={
                "baseline_blob": self.blobs.put(manifest.encode()),
                "baseline_files": len(baseline.files),
                "baseline_truncated": baseline.truncated,
            },
        )
        db.add(session)
        db.flush()
        return session

    def end_workspace_session(self, db: Session, session_id: int, outcome: str | None = None) -> dict[str, Any]:
        session = db.get(DevSession, session_id)
        if session is None:
            raise CaptureError("session not found")
        if session.status != "active":
            raise CaptureError("session is not active")
        project = session.project
        blob = session.meta.get("baseline_blob")
        raw = self.blobs.get(blob) if blob else None
        if raw is None:
            raise CaptureError("baseline for this session is missing")
        data = json.loads(raw)
        baseline = Baseline({p: FileState.from_json(v) for p, v in data["files"].items()}, bool(data.get("truncated")))
        root = Path(project.root_path)
        changes = compute_changes(root, baseline, self._policy(), self.blobs)
        for ch in changes:
            before = (
                self._snapshot(db, project.id, session.id, ch.path, ch.before_blob, "before")
                if ch.before_blob
                else None
            )
            after = (
                self._snapshot(db, project.id, session.id, ch.path, ch.after_blob, "after", ch.size, ch.redactions)
                if ch.after_blob
                else None
            )
            db.add(
                FileChange(
                    session_id=session.id,
                    path=ch.path,
                    change_type=ch.change_type,
                    old_path=ch.old_path,
                    before_snapshot_id=before.id if before else None,
                    after_snapshot_id=after.id if after else None,
                    diff=ch.diff,
                    lines_added=ch.lines_added,
                    lines_removed=ch.lines_removed,
                    source="workspace",
                )
            )
        session.status = "completed"
        session.ended_at = utcnow()
        session.git_head_end = git_head(root)
        if outcome:
            session.outcome = outcome
        db.flush()
        return {"session_id": session.id, "changes": len(changes)}

    def _snapshot(
        self,
        db: Session,
        project_id: int,
        session_id: int,
        path: str,
        blob: str | None,
        kind: str,
        size: int = 0,
        redactions: int = 0,
    ) -> FileSnapshot:
        text = self.blobs.get_text(blob) if blob else ""
        snap = FileSnapshot(
            project_id=project_id,
            session_id=session_id,
            path=path,
            content_hash=blob or "",
            size=size or len(text or ""),
            language=detect_language(path),
            kind=kind,
            redactions=redactions,
        )
        db.add(snap)
        db.flush()
        return snap

    # ----------------------------------------------------------------- manual entries
    def add_prompt(self, db: Session, session_id: int, text: str) -> UserPrompt:
        session = self._active_or_any(db, session_id)
        red = redact(_clip(text))
        seq = len(session.prompts) + 1
        row = UserPrompt(
            session_id=session.id,
            sequence=seq,
            content=red.text,
            content_hash=sha256_hex(text.encode()),
            redactions=red.count,
        )
        db.add(row)
        db.flush()
        return row

    def add_response(
        self,
        db: Session,
        session_id: int,
        text: str,
        model: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> CapturedResponse:
        session = self._active_or_any(db, session_id)
        red = redact(_clip(text))
        flags = [f.to_dict() for f in scan_untrusted(red.text)]
        prompt = session.prompts[-1] if session.prompts else None
        row = CapturedResponse(
            session_id=session.id,
            prompt_id=prompt.id if prompt else None,
            sequence=len(session.responses) + 1,
            model=model,
            content=red.text,
            tool_calls=tool_calls or [],
            injection_flags=flags,
            redactions=red.count,
        )
        db.add(row)
        db.flush()
        return row

    def _active_or_any(self, db: Session, session_id: int) -> DevSession:
        session = db.get(DevSession, session_id)
        if session is None:
            raise CaptureError("session not found")
        return session

    # ----------------------------------------------------------------- adapter import
    def discover(self, provider_key: str, project_root: Path | None) -> list[ExternalSession]:
        return self.adapters.get(provider_key).discover(project_root)

    def import_session(
        self, db: Session, project_id: int, provider_key: str, external_id: str, force: bool = False
    ) -> DevSession:
        project = self._project(db, project_id)
        adapter = self.adapters.get(provider_key)
        root = Path(project.root_path)
        refs = [r for r in adapter.discover(root) if r.external_id == external_id]
        if not refs:
            raise CaptureError(f"session '{external_id}' not found for provider '{provider_key}' in this project")
        provider = self.ensure_provider(db, provider_key)
        existing = db.execute(
            select(DevSession).where(DevSession.provider_id == provider.id, DevSession.external_id == external_id)
        ).scalar_one_or_none()
        if existing and not force:
            return existing
        if existing:
            db.delete(existing)
            db.flush()
        captured = adapter.load(refs[0])
        return self._persist_captured(db, project, provider, captured)

    def _persist_captured(
        self, db: Session, project: Project, provider: AIProvider, cap: CapturedSession
    ) -> DevSession:
        root = Path(project.root_path).resolve()
        policy = self._policy()
        session = DevSession(
            project_id=project.id,
            provider_id=provider.id,
            external_id=cap.meta.external_id,
            title=cap.meta.title or (cap.messages[0].text[:80] if cap.messages else "Imported session"),
            status="completed",
            capture_source="adapter",
            started_at=cap.meta.started_at or utcnow(),
            ended_at=cap.meta.ended_at,
            meta={
                "skipped": cap.skipped,
                "git_branch": cap.meta.git_branch,
                "source": Path(cap.meta.source_path).name,
                "imported_at": datetime.now(UTC).isoformat(),
            },
        )
        db.add(session)
        db.flush()
        seq_p = seq_r = 0
        redactions = 0
        for m in cap.messages:
            if m.role == "user":
                seq_p += 1
                red = redact(_clip(m.text))
                redactions += red.count
                db.add(
                    UserPrompt(
                        session_id=session.id,
                        sequence=seq_p,
                        content=red.text,
                        content_hash=sha256_hex(m.text.encode()),
                        redactions=red.count,
                        created_at=m.timestamp or utcnow(),
                    )
                )
            else:
                seq_r += 1
                red = redact(_clip(m.text))
                redactions += red.count
                tcs = [
                    {"name": t.name, "kind": t.kind, "summary": redact(t.summary).text[:200], "ok": t.ok}
                    for t in m.tool_calls
                ]
                db.add(
                    CapturedResponse(
                        session_id=session.id,
                        sequence=seq_r,
                        model=m.model,
                        content=red.text,
                        tool_calls=tcs,
                        injection_flags=[f.to_dict() for f in scan_untrusted(red.text)],
                        redactions=red.count,
                        created_at=m.timestamp or utcnow(),
                    )
                )
        db.flush()
        excluded = self._persist_edits(db, session, project, root, policy, cap.file_edits)
        session.meta = {
            **session.meta,
            "excluded_edits": excluded,
            "redactions": redactions,
            "observed_commands": [
                {"command": redact(c.command).text[:200], "ok": c.ok, "is_test": c.is_test} for c in cap.commands
            ][:100],
        }
        tests = [c for c in cap.commands if c.is_test and c.ok is not None]
        if tests:
            session.outcome = "passed" if tests[-1].ok else "failed"  # observed outcome only; NOT verification
        db.flush()
        return session

    def _persist_edits(
        self,
        db: Session,
        session: DevSession,
        project: Project,
        root: Path,
        policy: ExclusionPolicy,
        edits: list[FileEdit],
    ) -> int:
        grouped: dict[str, list[FileEdit]] = {}
        excluded = 0
        for e in edits:
            if not e.ok:
                continue
            rel = self._relative(root, e.path)
            if rel is None or policy.check_path(rel).excluded:
                excluded += 1
                continue
            grouped.setdefault(rel, []).append(e)
        for rel, es in grouped.items():
            created = es[0].kind == "create"
            diff_parts: list[str] = []
            added = removed = 0
            last_write: str | None = None
            for e in es:
                if e.content is not None:
                    last_write = e.content
                    a = e.content.count("\n") + (1 if e.content and not e.content.endswith("\n") else 0)
                    added += a
                    diff_parts.append(f"@@ write {rel} (assistant wrote {a} lines) @@\n")
                for old, new in e.edits:
                    removed += old.count("\n") + 1 if old else 0
                    added += new.count("\n") + 1 if new else 0
                    diff_parts.append(
                        f"@@ edit {rel} @@\n"
                        + "".join(f"-{ln}\n" for ln in old.splitlines())
                        + "".join(f"+{ln}\n" for ln in new.splitlines())
                    )
            diff = redact("".join(diff_parts)).text[:200_000]
            # Final content: what is on disk now (workspace truth), else the last full Write.
            full = root / rel
            final_bytes: bytes | None = None
            try:
                if full.is_file() and not full.is_symlink():
                    final_bytes = full.read_bytes()
            except OSError:
                final_bytes = None
            if final_bytes is None and last_write is not None:
                final_bytes = last_write.encode()
            after_snap = None
            if final_bytes is not None and not policy.check_content(final_bytes).excluded:
                red = redact(final_bytes.decode("utf-8", errors="replace"))
                after_snap = self._snapshot(
                    db,
                    project.id,
                    session.id,
                    rel,
                    self.blobs.put(red.text.encode()),
                    "after",
                    len(final_bytes),
                    red.count,
                )
            elif final_bytes is not None:
                excluded += 1
                continue
            db.add(
                FileChange(
                    session_id=session.id,
                    path=rel,
                    change_type="created" if created else "modified",
                    after_snapshot_id=after_snap.id if after_snap else None,
                    diff=diff,
                    lines_added=added,
                    lines_removed=removed,
                    source="adapter",
                )
            )
        return excluded

    @staticmethod
    def _relative(root: Path, abs_path: str) -> str | None:
        try:
            p = Path(abs_path)
            p = p if p.is_absolute() else root / p
            return p.resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            return None
