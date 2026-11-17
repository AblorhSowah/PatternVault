"""Knowledge export / import (JSON). Imported knowledge is untrusted: it re-enters at ``observed``."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.extraction.extractor import PatternCandidate
from app.models import KnowledgePattern, PatternRelationship
from app.security.secrets import redact_obj
from app.version import APP_VERSION

FORMAT = "patternvault-export"
FORMAT_VERSION = 1
MAX_IMPORT_PATTERNS = 5000
MAX_IMPORT_BYTES = 100_000_000


class ExportedVersion(BaseModel):
    version_number: int
    title: str
    description: str = ""
    content: dict[str, Any]
    content_hash: str
    change_note: str = ""
    created_by: str = "extraction"


class ExportedPattern(BaseModel):
    uid: str = Field(min_length=8, max_length=64)
    title: str = Field(min_length=1, max_length=512)
    summary: str = ""
    category: str = "general"
    tags: list[str] = []
    language: str | None = None
    framework: str | None = None
    framework_version: str | None = None
    status: str = "observed"
    fingerprint: str
    versions: list[ExportedVersion] = Field(min_length=1)
    relationships: list[dict[str, Any]] = []


class ExportFile(BaseModel):
    format: str
    version: int
    exported_at: str
    app_version: str = ""
    patterns: list[ExportedPattern]


class ImportError_(ValueError):
    pass


class Portability:
    def __init__(self, session_factory: sessionmaker[Session], patterns: Any, indexer: Any, audit: Any) -> None:
        self._factory = session_factory
        self._patterns = patterns
        self._indexer = indexer
        self._audit = audit

    def export(self, *, statuses: list[str] | None = None, pattern_ids: list[int] | None = None) -> dict[str, Any]:
        with self._factory() as db:
            stmt = select(KnowledgePattern).order_by(KnowledgePattern.id)
            if statuses:
                stmt = stmt.where(KnowledgePattern.status.in_(statuses))
            if pattern_ids:
                stmt = stmt.where(KnowledgePattern.id.in_(pattern_ids))
            pats = list(db.execute(stmt).scalars())
            uid_by_id = {p.id: p.uid for p in db.execute(select(KnowledgePattern)).scalars()}
            out = []
            for p in pats:
                rels = db.execute(select(PatternRelationship).where(PatternRelationship.source_id == p.id)).scalars()
                out.append(
                    {
                        "uid": p.uid,
                        "title": p.title,
                        "summary": p.summary,
                        "category": p.category,
                        "tags": p.tags,
                        "language": p.language,
                        "framework": p.framework,
                        "framework_version": p.framework_version,
                        "status": p.status,
                        "fingerprint": p.fingerprint,
                        "versions": [
                            {
                                "version_number": v.version_number,
                                "title": v.title,
                                "description": v.description,
                                "content": v.content,
                                "content_hash": v.content_hash,
                                "change_note": v.change_note,
                                "created_by": v.created_by,
                            }
                            for v in p.versions
                        ],
                        "relationships": [
                            {"target_uid": uid_by_id.get(r.target_id), "relation": r.relation, "score": r.score}
                            for r in rels
                            if r.target_id in uid_by_id
                        ],
                    }
                )
        # Defence in depth: redact again so a secret that slipped in earlier can never leave the machine.
        doc = redact_obj(
            {
                "format": FORMAT,
                "version": FORMAT_VERSION,
                "exported_at": datetime.now(UTC).isoformat(),
                "app_version": APP_VERSION,
                "patterns": out,
            }
        )
        self._audit.record("user", "knowledge.export", resource_type="knowledge", detail={"patterns": len(out)})
        return doc

    def import_(self, raw: bytes | str | dict[str, Any]) -> dict[str, Any]:
        if isinstance(raw, bytes | str):
            if len(raw) > MAX_IMPORT_BYTES:
                raise ImportError_("import file is too large")
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ImportError_(f"not valid JSON: {exc}") from exc
        try:
            doc = ExportFile.model_validate(raw)
        except ValidationError as exc:
            raise ImportError_(
                f"invalid export file: {exc.errors()[0]['msg']} at {'.'.join(map(str, exc.errors()[0]['loc']))}"
            ) from exc
        if doc.format != FORMAT or doc.version > FORMAT_VERSION:
            raise ImportError_(f"unsupported format '{doc.format}' v{doc.version}")
        if len(doc.patterns) > MAX_IMPORT_PATTERNS:
            raise ImportError_("too many patterns in one import")
        created = skipped = 0
        id_by_uid: dict[str, int] = {}
        with self._factory() as db:
            existing_uids = {p.uid: p.id for p in db.execute(select(KnowledgePattern)).scalars()}
            existing_fp = {p.fingerprint: p.id for p in db.execute(select(KnowledgePattern)).scalars()}
        for ep in doc.patterns:
            if ep.uid in existing_uids or ep.fingerprint in existing_fp:
                skipped += 1
                id_by_uid[ep.uid] = existing_uids.get(ep.uid) or existing_fp[ep.fingerprint]
                continue
            v = sorted(ep.versions, key=lambda x: x.version_number)[-1]
            clean = redact_obj(v.content)
            cand = PatternCandidate(
                title=ep.title,
                summary=ep.summary,
                category=ep.category,
                language=ep.language,
                framework=ep.framework,
                framework_version=ep.framework_version,
                tags=ep.tags,
                content=clean,
                fingerprint=ep.fingerprint,
                content_hash=v.content_hash,
                provenance={
                    "imported": True,
                    "imported_status": ep.status,
                    "original_uid": ep.uid,
                    "imported_at": datetime.now(UTC).isoformat(),
                },
            )
            # Evidence is not transferable: nothing imported may arrive tested/verified/approved.
            res = self._patterns.add_candidate(cand, project_id=None, session_id=None, actor="user:import")
            id_by_uid[ep.uid] = res.pattern_id
            created += 1
        for ep in doc.patterns:
            for rel in ep.relationships:
                src, dst = id_by_uid.get(ep.uid), id_by_uid.get(str(rel.get("target_uid")))
                if src and dst and src != dst:
                    try:
                        self._patterns.relate(src, dst, str(rel.get("relation")), float(rel.get("score", 0.0)))
                    except ValueError:
                        continue
        self._audit.record(
            "user", "knowledge.import", resource_type="knowledge", detail={"created": created, "skipped": skipped}
        )
        return {"created": created, "skipped": skipped, "total": len(doc.patterns)}
