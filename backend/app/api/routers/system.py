"""Health, dashboard summary, operating mode and audit endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.deps import Authed, Db, Svc
from app.api.schemas import AuditOut, Health, ModeRequest
from app.config import ConfigError
from app.models import (
    AgentExecution,
    AuditLog,
    CodeImplementation,
    DevSession,
    KnowledgePattern,
    Project,
    Task,
)
from app.security.audit import verify_chain
from app.version import API_VERSION, APP_VERSION


def _local_model(svc: Any) -> dict[str, Any]:
    """Probe the local model server (runs in the request's worker thread, so a private event loop is fine)."""
    import asyncio

    from app.api.routers.generation import model_status

    st = asyncio.run(model_status(svc))
    return {k: st[k] for k in ("reachable", "ready", "version", "selected", "embedding_selected", "reason", "running")}


public = APIRouter(tags=["system"])
router = APIRouter(tags=["system"], dependencies=[Authed])


@public.get("/health", response_model=Health)
def health() -> Health:
    """Unauthenticated liveness probe (exposes nothing sensitive)."""
    return Health(status="ok", version=APP_VERSION, api_version=API_VERSION)


@router.get("/dashboard")
def dashboard(db: Db, svc: Svc) -> dict[str, Any]:
    cfg = svc.config.config
    count = lambda model, *where: db.scalar(select(func.count()).select_from(model).where(*where)) or 0  # noqa: E731
    recent_tasks = db.execute(select(Task).order_by(Task.id.desc()).limit(8)).scalars().all()
    recent_sessions = db.execute(select(DevSession).order_by(DevSession.id.desc()).limit(8)).scalars().all()
    running = db.execute(select(AgentExecution).where(AgentExecution.status == "running")).scalars().all()
    return {
        "mode": cfg.mode.value,
        "auto_fallback": cfg.auto_fallback,
        "permission_level": cfg.permission_level.value,
        "counts": {
            "projects": count(Project),
            "sessions": count(DevSession),
            "patterns": count(KnowledgePattern),
            "verified_patterns": count(KnowledgePattern, KnowledgePattern.status == "verified"),
            "generated_implementations": count(
                CodeImplementation, CodeImplementation.kind.in_(("generated", "adapted", "replayed"))
            ),
        },
        "agents_running": [
            {"agent": e.agent_key, "task_id": e.task_id, "step": e.step_key, "started_at": e.started_at.isoformat()}
            for e in running
        ],
        "recent_tasks": [
            {
                "id": t.id,
                "kind": t.kind,
                "request": t.request[:140],
                "status": t.status,
                "verified": t.verified,
                "created_at": t.created_at.isoformat(),
            }
            for t in recent_tasks
        ],
        "recent_sessions": [
            {
                "id": s.id,
                "title": s.title,
                "status": s.status,
                "outcome": s.outcome,
                "started_at": s.started_at.isoformat(),
            }
            for s in recent_sessions
        ],
        "local_model": _local_model(svc),
        "uptime_s": round(__import__("time").monotonic() - svc.started_monotonic, 1),
    }


@router.get("/mode")
def get_mode(svc: Svc) -> dict[str, Any]:
    cfg = svc.config.config
    return {"mode": cfg.mode.value, "auto_fallback": cfg.auto_fallback}


@router.put("/mode")
def set_mode(body: ModeRequest, svc: Svc) -> dict[str, Any]:
    previous = svc.config.config.mode
    try:
        cfg = svc.config.update({"mode": body.mode.value})
    except ConfigError as exc:
        from fastapi import HTTPException

        raise HTTPException(422, str(exc)) from exc
    svc.audit.record(
        "user",
        "mode.change",
        resource_type="config",
        detail={"from": previous.value, "to": body.mode.value},
        permission_level=cfg.permission_level.value,
    )
    svc.events.publish("mode.changed", {"mode": cfg.mode.value, "previous": previous.value})
    return {"mode": cfg.mode.value, "auto_fallback": cfg.auto_fallback}


@router.get("/audit", response_model=list[AuditOut])
def list_audit(
    db: Db,
    limit: int = Query(100, ge=1, le=1000),
    action: str | None = None,
    before_id: int | None = None,
) -> list[AuditLog]:
    q = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    if action:
        q = q.where(AuditLog.action.like(f"{action}%"))
    if before_id:
        q = q.where(AuditLog.id < before_id)
    return list(db.execute(q).scalars())


@router.get("/audit/verify")
def audit_verify(db: Db) -> dict[str, Any]:
    return verify_chain(db)
