"""PatternService: persistence, versioning, dedupe, lifecycle, search and deletion of knowledge patterns.

Each public method runs in its own transaction, then audits and publishes events *after* commit
(see AuditLogger: no open write transaction while auditing).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.extraction.extractor import PatternCandidate, content_hash_of
from app.knowledge import fts
from app.knowledge.lifecycle import LifecycleError, compute_confidence, transition
from app.models import (
    Dependency,
    KnowledgePattern,
    PatternRelationship,
    PatternVersion,
    UserFeedback,
)
from app.models.base import utcnow
from app.models.knowledge import PATTERN_RELATIONS
from app.security.secrets import redact, redact_obj


class PatternNotFound(LookupError):
    pass


@dataclass
class StoreResult:
    pattern_id: int
    action: str  # created | new_version | duplicate
    version: int


def _body(content: dict[str, Any], candidate_text: str = "") -> str:
    parts = [content.get("request", ""), candidate_text]
    for f in content.get("files", []):
        parts.append(f.get("path", ""))
        parts.extend(s.get("name", "") for s in f.get("symbols", []))
        parts.extend(f"{r['method']} {r['path']}" for r in f.get("routes", []))
        parts.append(f.get("content", "")[:20_000])
    parts.extend(d.get("name", "") for d in content.get("dependencies", []))
    return "\n".join(str(p) for p in parts)


class PatternService:
    def __init__(self, session_factory: sessionmaker[Session], audit: Any, events: Any) -> None:
        self._factory = session_factory
        self._audit = audit
        self._events = events
        self.listeners: list[Any] = []  # callables(event: str, pattern_id: int) → used by the vector indexer

    # ------------------------------------------------------------------ create
    def add_candidate(
        self,
        cand: PatternCandidate,
        *,
        project_id: int | None,
        session_id: int | None,
        status: str = "observed",
        actor: str = "agent:extraction",
    ) -> StoreResult:
        # Defence in depth: the store never trusts callers (extractor, import, agents) to have redacted already.
        cand.content = redact_obj(cand.content)
        cand.title, cand.summary = redact(cand.title).text, redact(cand.summary).text
        cand.content_hash = content_hash_of(cand.content.get("files", []))
        with self._factory() as db:
            existing = (
                db.execute(
                    select(KnowledgePattern).where(
                        KnowledgePattern.fingerprint == cand.fingerprint, KnowledgePattern.status != "deprecated"
                    )
                )
                .scalars()
                .first()
            )
            if existing and existing.current_version and existing.current_version.content_hash == cand.content_hash:
                occ = list(existing.provenance.get("occurrences", []))
                occ.append({"session_id": session_id, "at": utcnow().isoformat()})
                existing.provenance = {**existing.provenance, "occurrences": occ[-50:]}
                existing.use_count += 0
                db.commit()
                result = StoreResult(existing.id, "duplicate", existing.current_version.version_number)
            elif existing:
                v = self._add_version(db, existing, cand, created_by="extraction")
                db.commit()
                result = StoreResult(existing.id, "new_version", v.version_number)
            else:
                p = KnowledgePattern(
                    title=cand.title,
                    summary=cand.summary,
                    category=cand.category,
                    tags=cand.tags,
                    language=cand.language,
                    framework=cand.framework,
                    framework_version=cand.framework_version,
                    status=status,
                    confidence=compute_confidence(status, 0, 0),
                    fingerprint=cand.fingerprint,
                    project_id=project_id,
                    origin_session_id=session_id,
                    provenance={**cand.provenance, "origin": "extraction"},
                )
                db.add(p)
                db.flush()
                v = self._add_version(db, p, cand, created_by="extraction", first=True)
                for d in cand.dependencies:
                    db.add(
                        Dependency(
                            pattern_id=p.id,
                            ecosystem=d.ecosystem,
                            name=d.name,
                            version_spec=d.version_spec,
                            is_dev=d.is_dev,
                            source_file=d.source_file,
                        )
                    )
                db.commit()
                result = StoreResult(p.id, "created", v.version_number)
            pid = result.pattern_id
        self._audit.record(
            actor,
            f"pattern.{result.action}",
            resource_type="pattern",
            resource_id=pid,
            detail={"session_id": session_id, "version": result.version, "category": cand.category},
        )
        self._events.publish("pattern." + result.action, {"id": pid, "title": cand.title})
        self._notify("changed", pid)
        return result

    def _add_version(
        self, db: Session, p: KnowledgePattern, cand: PatternCandidate, *, created_by: str, first: bool = False
    ) -> PatternVersion:
        number = (
            db.scalar(select(func.max(PatternVersion.version_number)).where(PatternVersion.pattern_id == p.id)) or 0
        ) + 1
        v = PatternVersion(
            pattern_id=p.id,
            version_number=number,
            title=cand.title,
            description=cand.summary,
            content=cand.content,
            content_hash=cand.content_hash,
            created_by=created_by,
            change_note="initial extraction" if first else "content changed in a later observed session",
            parent_version_id=p.current_version_id,
        )
        db.add(v)
        db.flush()
        p.current_version_id = v.id
        if not first:
            p.title, p.summary = cand.title, cand.summary
            if p.status in ("tested", "verified", "approved"):
                # new content invalidates evidence gathered for the previous version
                p.status = "observed"
                p.verified_at = None
                p.verified_by_testing_session_id = None
                p.confidence = compute_confidence("observed", p.success_count, p.failure_count)
                p.provenance = {**p.provenance, "approved_by_user": False}
        self._index(db, p, v)
        return v

    def _index(self, db: Session, p: KnowledgePattern, v: PatternVersion) -> None:
        fts.index_pattern(db, p.id, p.title, p.summary, " ".join(p.tags or []), _body(v.content))

    # ------------------------------------------------------------------ read
    def get(self, pattern_id: int, include_content: bool = True) -> dict[str, Any]:
        with self._factory() as db:
            p = db.get(KnowledgePattern, pattern_id)
            if p is None:
                raise PatternNotFound(pattern_id)
            return self._to_dict(db, p, include_content)

    def _to_dict(self, db: Session, p: KnowledgePattern, include_content: bool) -> dict[str, Any]:
        v = p.current_version
        d: dict[str, Any] = {
            "id": p.id,
            "uid": p.uid,
            "title": p.title,
            "summary": p.summary,
            "category": p.category,
            "tags": p.tags,
            "language": p.language,
            "framework": p.framework,
            "framework_version": p.framework_version,
            "status": p.status,
            "confidence": p.confidence,
            "project_id": p.project_id,
            "origin_session_id": p.origin_session_id,
            "use_count": p.use_count,
            "success_count": p.success_count,
            "failure_count": p.failure_count,
            "provenance": p.provenance,
            "version": v.version_number if v else 0,
            "verified_at": p.verified_at.isoformat() if p.verified_at else None,
            "created_at": p.created_at.isoformat(),
            "updated_at": p.updated_at.isoformat(),
            "file_count": len(v.content.get("files", [])) if v else 0,
            "user_approved": bool(p.provenance.get("approved_by_user")),
        }
        if include_content and v:
            d["content"] = v.content
        return d

    def list_patterns(
        self,
        *,
        status: str | None = None,
        category: str | None = None,
        language: str | None = None,
        framework: str | None = None,
        project_id: int | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        with self._factory() as db:
            stmt = select(KnowledgePattern)
            if status:
                stmt = stmt.where(KnowledgePattern.status == status)
            if category:
                stmt = stmt.where(KnowledgePattern.category == category)
            if language:
                stmt = stmt.where(KnowledgePattern.language == language)
            if framework:
                stmt = stmt.where(KnowledgePattern.framework == framework)
            if project_id:
                stmt = stmt.where(KnowledgePattern.project_id == project_id)
            order: list[Any] = [KnowledgePattern.confidence.desc(), KnowledgePattern.id.desc()]
            if q:
                hits = fts.search(db, q, limit=500)
                ids = [h[0] for h in hits]
                stmt = stmt.where(KnowledgePattern.id.in_(ids)) if ids else stmt.where(KnowledgePattern.id == -1)
                rank = {pid: i for i, pid in enumerate(ids)}
                total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
                rows = list(db.execute(stmt).scalars())
                rows.sort(key=lambda p: rank.get(p.id, 10**9))
                rows = rows[offset : offset + limit]
            else:
                total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
                rows = list(db.execute(stmt.order_by(*order).limit(limit).offset(offset)).scalars())
            return {"total": total, "items": [self._to_dict(db, p, False) for p in rows]}

    def versions(self, pattern_id: int) -> list[dict[str, Any]]:
        with self._factory() as db:
            if db.get(KnowledgePattern, pattern_id) is None:
                raise PatternNotFound(pattern_id)
            rows = db.execute(
                select(PatternVersion)
                .where(PatternVersion.pattern_id == pattern_id)
                .order_by(PatternVersion.version_number)
            ).scalars()
            return [
                {
                    "id": v.id,
                    "version": v.version_number,
                    "title": v.title,
                    "change_note": v.change_note,
                    "created_by": v.created_by,
                    "created_at": v.created_at.isoformat(),
                    "content_hash": v.content_hash,
                    "files": [f["path"] for f in v.content.get("files", [])],
                }
                for v in rows
            ]

    def facets(self) -> dict[str, Any]:
        with self._factory() as db:

            def counts(col: Any) -> dict[str, int]:
                return {str(k): n for k, n in db.execute(select(col, func.count()).group_by(col)).all() if k}

            return {
                "categories": counts(KnowledgePattern.category),
                "languages": counts(KnowledgePattern.language),
                "frameworks": counts(KnowledgePattern.framework),
                "statuses": counts(KnowledgePattern.status),
            }

    # ------------------------------------------------------------------ mutate
    def update_metadata(
        self,
        pattern_id: int,
        *,
        title: str | None = None,
        summary: str | None = None,
        tags: list[str] | None = None,
        category: str | None = None,
        actor: str = "user",
    ) -> dict[str, Any]:
        with self._factory() as db:
            p = self._must(db, pattern_id)
            changes: dict[str, Any] = {}
            for k, v in (("title", title), ("summary", summary), ("tags", tags), ("category", category)):
                if v is not None and getattr(p, k) != v:
                    changes[k] = v
                    setattr(p, k, v)
            if changes and p.current_version:
                self._index(db, p, p.current_version)
            db.commit()
            out = self._to_dict(db, p, False)
        if changes:
            self._audit.record(actor, "pattern.update", resource_type="pattern", resource_id=pattern_id, detail=changes)
            self._notify("changed", pattern_id)
        return out

    def approve(self, pattern_id: int, comment: str = "", actor: str = "user") -> dict[str, Any]:
        with self._factory() as db:
            p = self._must(db, pattern_id)
            if p.status == "deprecated":
                raise LifecycleError("restore the pattern before approving it")
            fb = UserFeedback(target_type="pattern", target_id=p.id, action="approve", comment=comment)
            db.add(fb)
            p.provenance = {**p.provenance, "approved_by_user": True, "approved_at": utcnow().isoformat()}
            if p.status in ("observed", "generated"):
                transition(db, p, "approved", rating_sum=self._rating_sum(db, p.id))
            db.commit()
            out = self._to_dict(db, p, False)
        self._audit.record(actor, "pattern.approve", resource_type="pattern", resource_id=pattern_id)
        self._events.publish("pattern.approved", {"id": pattern_id})
        return out

    def deprecate(self, pattern_id: int, comment: str = "", actor: str = "user") -> dict[str, Any]:
        with self._factory() as db:
            p = self._must(db, pattern_id)
            db.add(UserFeedback(target_type="pattern", target_id=p.id, action="deprecate", comment=comment))
            transition(db, p, "deprecated")
            db.commit()
            out = self._to_dict(db, p, False)
        self._audit.record(
            actor, "pattern.deprecate", resource_type="pattern", resource_id=pattern_id, detail={"comment": comment}
        )
        self._notify("changed", pattern_id)
        return out

    def restore(self, pattern_id: int, actor: str = "user") -> dict[str, Any]:
        with self._factory() as db:
            p = self._must(db, pattern_id)
            transition(db, p, "observed")
            db.commit()
            out = self._to_dict(db, p, False)
        self._audit.record(actor, "pattern.restore", resource_type="pattern", resource_id=pattern_id)
        return out

    def promote(
        self, pattern_id: int, target: str, testing_session_id: int, actor: str = "agent:knowledge"
    ) -> dict[str, Any]:
        """Move to ``tested``/``verified`` - only with passing test evidence (see lifecycle.transition)."""
        if target not in ("tested", "verified"):
            raise LifecycleError("promote() only promotes to 'tested' or 'verified'")
        with self._factory() as db:
            p = self._must(db, pattern_id)
            try:
                prev = transition(
                    db, p, target, testing_session_id=testing_session_id, rating_sum=self._rating_sum(db, p.id)
                )
            except LifecycleError as exc:
                db.rollback()
                err = str(exc)
            else:
                db.commit()
                err = ""
                out = self._to_dict(db, p, False)
        if err:
            self._audit.record(
                actor,
                f"pattern.promote.{target}",
                resource_type="pattern",
                resource_id=pattern_id,
                outcome="denied",
                detail={"reason": err, "testing_session_id": testing_session_id},
            )
            raise LifecycleError(err)
        self._audit.record(
            actor,
            f"pattern.promote.{target}",
            resource_type="pattern",
            resource_id=pattern_id,
            detail={"from": prev, "testing_session_id": testing_session_id},
        )
        self._events.publish("pattern.status", {"id": pattern_id, "status": target})
        return out

    def add_feedback(
        self,
        pattern_id: int,
        *,
        rating: int | None,
        comment: str = "",
        action: str = "rate",
        correction: dict[str, Any] | None = None,
        actor: str = "user",
    ) -> dict[str, Any]:
        with self._factory() as db:
            p = self._must(db, pattern_id)
            db.add(
                UserFeedback(
                    target_type="pattern",
                    target_id=p.id,
                    action=action,
                    rating=rating,
                    comment=comment,
                    correction=correction or {},
                )
            )
            db.flush()
            p.confidence = compute_confidence(p.status, p.success_count, p.failure_count, self._rating_sum(db, p.id))
            db.commit()
            out = self._to_dict(db, p, False)
        self._audit.record(
            actor,
            "pattern.feedback",
            resource_type="pattern",
            resource_id=pattern_id,
            detail={"action": action, "rating": rating},
        )
        return out

    def record_use(self, pattern_id: int, *, success: bool | None, count_use: bool = True) -> None:
        """Record a reuse. ``count_use=False`` records only the *outcome* of a use that was already counted
        (e.g. generation counted the use; the verification result arrives later)."""
        with self._factory() as db:
            p = self._must(db, pattern_id)
            if count_use:
                p.use_count += 1
            p.last_used_at = utcnow()
            if success is True:
                p.success_count += 1
            elif success is False:
                p.failure_count += 1
            p.confidence = compute_confidence(p.status, p.success_count, p.failure_count, self._rating_sum(db, p.id))
            db.commit()

    def relate(self, source_id: int, target_id: int, relation: str, score: float = 0.0, note: str = "") -> None:
        if relation not in PATTERN_RELATIONS:
            raise ValueError(f"unknown relation '{relation}'")
        if source_id == target_id:
            raise ValueError("a pattern cannot relate to itself")
        with self._factory() as db:
            self._must(db, source_id)
            self._must(db, target_id)
            exists = db.execute(
                select(PatternRelationship).where(
                    PatternRelationship.source_id == source_id,
                    PatternRelationship.target_id == target_id,
                    PatternRelationship.relation == relation,
                )
            ).scalar_one_or_none()
            if exists:
                exists.score, exists.note = score, note
            else:
                db.add(
                    PatternRelationship(
                        source_id=source_id, target_id=target_id, relation=relation, score=score, note=note
                    )
                )
            db.commit()

    def delete(self, pattern_id: int, actor: str = "user") -> None:
        with self._factory() as db:
            p = self._must(db, pattern_id)
            title = p.title
            p.current_version_id = None
            db.flush()
            fts.remove_pattern(db, pattern_id)
            db.execute(
                delete(UserFeedback).where(UserFeedback.target_type == "pattern", UserFeedback.target_id == pattern_id)
            )
            db.delete(p)
            db.commit()
        self._audit.record(
            actor, "pattern.delete", resource_type="pattern", resource_id=pattern_id, detail={"title": title}
        )
        self._events.publish("pattern.deleted", {"id": pattern_id})
        self._notify("deleted", pattern_id)

    # ------------------------------------------------------------------ helpers
    def _must(self, db: Session, pattern_id: int) -> KnowledgePattern:
        p = db.get(KnowledgePattern, pattern_id)
        if p is None:
            raise PatternNotFound(pattern_id)
        return p

    @staticmethod
    def _rating_sum(db: Session, pattern_id: int) -> int:
        return int(
            db.scalar(
                select(func.coalesce(func.sum(UserFeedback.rating), 0)).where(
                    UserFeedback.target_type == "pattern", UserFeedback.target_id == pattern_id
                )
            )
            or 0
        )

    def _notify(self, event: str, pattern_id: int) -> None:
        for fn in self.listeners:
            try:
                fn(event, pattern_id)
            except Exception:  # listener failures (e.g. vector index) must not break knowledge writes
                import logging

                logging.getLogger("patternvault").exception("pattern listener failed")
