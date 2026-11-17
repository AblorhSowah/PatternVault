"""LearningService: project analysis and session → pattern extraction."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from app.extraction.analyzer import analyze_project
from app.extraction.extractor import FileInput, SessionInput, extract_candidates
from app.knowledge.service import PatternService, StoreResult
from app.learning.blobs import BlobStore
from app.models import Dependency, DevSession, Project
from app.models.base import utcnow
from app.security.exclusions import ExclusionPolicy


class LearningService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        blobs: BlobStore,
        patterns: PatternService,
        audit: Any,
        events: Any,
        policy_factory: Any,
    ) -> None:
        self._factory = session_factory
        self.blobs = blobs
        self.patterns = patterns
        self.audit = audit
        self.events = events
        self._policy_factory = policy_factory

    def analyze_project(self, project_id: int) -> dict[str, Any]:
        with self._factory() as db:
            project = db.get(Project, project_id)
            if project is None:
                raise LookupError("project not found")
            root = Path(project.root_path)
            if not root.is_dir():
                raise LookupError("project root no longer exists")
            policy: ExclusionPolicy = self._policy_factory()
            analysis = analyze_project(root, policy)
            project.languages = analysis.languages
            project.frameworks = analysis.frameworks
            project.conventions = {
                **analysis.conventions,
                "primary_language": analysis.primary_language,
                "analyzed_at": utcnow().isoformat(),
            }
            db.execute(delete(Dependency).where(Dependency.project_id == project.id))
            for d in analysis.dependencies:
                db.add(
                    Dependency(
                        project_id=project.id,
                        ecosystem=d.ecosystem,
                        name=d.name,
                        version_spec=d.version_spec,
                        is_dev=d.is_dev,
                        source_file=d.source_file,
                    )
                )
            db.commit()
            out: dict[str, Any] = {
                "project_id": project.id,
                "file_count": analysis.file_count,
                "primary_language": analysis.primary_language,
                "languages": analysis.languages,
                "frameworks": analysis.frameworks,
                "conventions": project.conventions,
                "dependency_count": len(analysis.dependencies),
            }
        self.audit.record(
            "system",
            "project.analyze",
            resource_type="project",
            resource_id=project_id,
            detail={"files": out["file_count"], "frameworks": [f["name"] for f in out["frameworks"]]},
        )
        return out

    def extract_session(self, session_id: int) -> list[StoreResult]:
        with self._factory() as db:
            s = db.get(DevSession, session_id)
            if s is None:
                raise LookupError("session not found")
            project = s.project
            files: list[FileInput] = []
            for ch in s.file_changes:
                after = (
                    self.blobs.get_text(ch.after_snapshot.content_hash)
                    if ch.after_snapshot and ch.after_snapshot.content_hash
                    else None
                )
                before = (
                    self.blobs.get_text(ch.before_snapshot.content_hash)
                    if ch.before_snapshot and ch.before_snapshot.content_hash
                    else None
                )
                files.append(FileInput(ch.path, ch.change_type, after, before, ch.diff))
            primary = (project.conventions or {}).get("primary_language")
            data = SessionInput(
                prompts=[p.content for p in s.prompts],
                files=files,
                commands=list(s.meta.get("observed_commands", [])),
                project_language=primary,
                project_frameworks=list(project.frameworks or []),
                provider=s.provider.key if s.provider else None,
                session_id=s.id,
                model=next((r.model for r in s.responses if r.model), None),
            )
            project_id = project.id
        candidates = extract_candidates(data)
        results = [self.patterns.add_candidate(c, project_id=project_id, session_id=session_id) for c in candidates]
        with self._factory() as db:
            s = db.get(DevSession, session_id)
            if s is not None:
                s.meta = {
                    **s.meta,
                    "extracted_pattern_ids": [r.pattern_id for r in results],
                    "extracted_at": utcnow().isoformat(),
                }
                db.commit()
        self.events.publish(
            "session.extracted", {"session_id": session_id, "patterns": [r.pattern_id for r in results]}
        )
        return results
