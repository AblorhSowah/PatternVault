"""Providers, capture sessions, extraction and project analysis."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select

from app.api.deps import Authed, Db, Svc
from app.api.schemas import ImportRequest, SessionEnd, SessionStart, TextEntry
from app.knowledge.storage import StorageLimitExceeded
from app.learning.adapters.base import UnsupportedOperation
from app.learning.capture import CaptureError
from app.models import DevSession, Project

router = APIRouter(tags=["learning"], dependencies=[Authed])


# ------------------------------------------------------------------ providers
@router.get("/providers")
def list_providers(svc: Svc) -> list[dict[str, Any]]:
    cfg = svc.config.config
    out = []
    for a in svc.adapters.all():
        d = a.describe()
        conf = cfg.providers.get(a.key)
        d["enabled"] = conf.enabled if conf else True
        out.append(d)
    return out


@router.get("/providers/{key}/sessions")
def provider_sessions(key: str, svc: Svc, db: Db, project_id: int | None = None) -> list[dict[str, Any]]:
    root: Path | None = None
    if project_id is not None:
        project = db.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "project not found")
        root = Path(project.root_path)
    try:
        sessions = svc.capture.discover(key, root)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except UnsupportedOperation as exc:
        raise HTTPException(422, str(exc)) from exc
    return [s.to_dict() for s in sessions]


@router.post("/providers/{key}/import", status_code=201)
def import_session(key: str, body: ImportRequest, db: Db, svc: Svc) -> dict[str, Any]:
    cfg = svc.config.config
    if not cfg.privacy.capture_enabled:
        raise HTTPException(403, "capture is disabled in privacy settings")
    if key in cfg.providers and not cfg.providers[key].enabled:
        raise HTTPException(403, f"provider '{key}' is disabled")
    try:
        session = svc.capture.import_session(db, body.project_id, key, body.external_id, force=body.force)
        db.commit()
        sid, title, files = session.id, session.title, len(session.file_changes)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (CaptureError, UnsupportedOperation) as exc:
        raise HTTPException(422, str(exc)) from exc
    svc.audit.record(
        "user", "session.import", resource_type="session", resource_id=sid, detail={"provider": key, "files": files}
    )
    svc.events.publish("session.imported", {"id": sid, "title": title})
    result: dict[str, Any] = {"session_id": sid, "title": title, "file_changes": files}
    if body.extract:
        result["patterns"] = [r.__dict__ for r in svc.learning.extract_session(sid)]
    return result


# ------------------------------------------------------------------ sessions
def _session_summary(s: DevSession) -> dict[str, Any]:
    return {
        "id": s.id,
        "project_id": s.project_id,
        "provider": s.provider.key if s.provider else None,
        "title": s.title,
        "status": s.status,
        "outcome": s.outcome,
        "capture_source": s.capture_source,
        "started_at": s.started_at.isoformat(),
        "ended_at": s.ended_at.isoformat() if s.ended_at else None,
        "prompts": len(s.prompts),
        "responses": len(s.responses),
        "file_changes": len(s.file_changes),
        "extracted_pattern_ids": s.meta.get("extracted_pattern_ids", []),
    }


@router.get("/sessions")
def list_sessions(
    db: Db,
    project_id: int | None = None,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    stmt = select(DevSession)
    if project_id:
        stmt = stmt.where(DevSession.project_id == project_id)
    if status:
        stmt = stmt.where(DevSession.status == status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(stmt.order_by(DevSession.started_at.desc()).limit(limit).offset(offset)).scalars().all()
    return {"total": total, "items": [_session_summary(s) for s in rows]}


@router.get("/sessions/{session_id}")
def get_session(session_id: int, db: Db) -> dict[str, Any]:
    s = db.get(DevSession, session_id)
    if s is None:
        raise HTTPException(404, "session not found")
    d = _session_summary(s)
    d["prompts_list"] = [
        {"sequence": p.sequence, "content": p.content[:4000], "redactions": p.redactions} for p in s.prompts
    ]
    d["responses_list"] = [
        {
            "sequence": r.sequence,
            "model": r.model,
            "content": r.content[:4000],
            "tool_calls": r.tool_calls,
            "injection_flags": r.injection_flags,
        }
        for r in s.responses
    ]
    d["changes"] = [
        {
            "path": c.path,
            "change_type": c.change_type,
            "lines_added": c.lines_added,
            "lines_removed": c.lines_removed,
            "source": c.source,
            "diff": c.diff[:20000],
        }
        for c in s.file_changes
    ]
    d["meta"] = {k: v for k, v in s.meta.items() if k != "baseline_blob"}
    return d


@router.post("/sessions", status_code=201)
def start_session(body: SessionStart, db: Db, svc: Svc) -> dict[str, Any]:
    if not svc.config.config.privacy.capture_enabled:
        raise HTTPException(403, "capture is disabled in privacy settings")
    try:
        s = svc.capture.start_workspace_session(db, body.project_id, body.title, body.provider)
        db.commit()
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except StorageLimitExceeded as exc:
        raise HTTPException(507, str(exc)) from exc
    except CaptureError as exc:
        raise HTTPException(422, str(exc)) from exc
    svc.audit.record(
        "user",
        "session.start",
        resource_type="session",
        resource_id=s.id,
        detail={"files_in_baseline": s.meta.get("baseline_files")},
    )
    svc.events.publish("session.started", {"id": s.id})
    return _session_summary(s)


@router.post("/sessions/{session_id}/end")
def end_session(session_id: int, body: SessionEnd, db: Db, svc: Svc) -> dict[str, Any]:
    try:
        summary = svc.capture.end_workspace_session(db, session_id, body.outcome)
        db.commit()
    except CaptureError as exc:
        raise HTTPException(422, str(exc)) from exc
    svc.audit.record("user", "session.end", resource_type="session", resource_id=session_id, detail=summary)
    svc.events.publish("session.ended", summary)
    if body.extract:
        summary["patterns"] = [r.__dict__ for r in svc.learning.extract_session(session_id)]
    return summary


@router.post("/sessions/{session_id}/prompts", status_code=201)
def add_prompt(session_id: int, body: TextEntry, db: Db, svc: Svc) -> dict[str, Any]:
    try:
        row = svc.capture.add_prompt(db, session_id, body.text)
    except CaptureError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"id": row.id, "sequence": row.sequence, "redactions": row.redactions}


@router.post("/sessions/{session_id}/responses", status_code=201)
def add_response(session_id: int, body: TextEntry, db: Db, svc: Svc) -> dict[str, Any]:
    try:
        row = svc.capture.add_response(db, session_id, body.text, body.model)
    except CaptureError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {
        "id": row.id,
        "sequence": row.sequence,
        "redactions": row.redactions,
        "injection_flags": row.injection_flags,
    }


@router.post("/sessions/{session_id}/extract")
def extract(session_id: int, svc: Svc) -> dict[str, Any]:
    try:
        results = svc.learning.extract_session(session_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"patterns": [r.__dict__ for r in results]}


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: int, db: Db, svc: Svc, confirm: bool = False) -> None:
    if not confirm:
        raise HTTPException(400, "destructive operation: pass confirm=true")
    s = db.get(DevSession, session_id)
    if s is None:
        raise HTTPException(404, "session not found")
    db.delete(s)
    db.commit()
    svc.audit.record("user", "session.delete", resource_type="session", resource_id=session_id)


# ------------------------------------------------------------------ project analysis
@router.post("/projects/{project_id}/analyze")
def analyze(project_id: int, svc: Svc) -> dict[str, Any]:
    try:
        return svc.learning.analyze_project(project_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
