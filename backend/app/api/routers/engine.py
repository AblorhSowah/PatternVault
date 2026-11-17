"""Tasks (orchestrator), agents, testing sessions, approvals and recovery points."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select

from app.agents.registry import TESTING_AGENT_KEYS
from app.api.deps import Authed, Db, Svc
from app.api.schemas import AgentUpdate, ApprovalDecision, BackupCreate, BackupRestore, TaskIn, TestRunIn
from app.execution.recovery import RecoveryError
from app.knowledge.backup import BackupError
from app.models import Project, TestingSession, TestResult
from app.orchestration.orchestrator import TaskRequest

router = APIRouter(tags=["engine"], dependencies=[Authed])


# ------------------------------------------------------------------ tasks
@router.post("/tasks", status_code=202)
async def submit_task(body: TaskIn, svc: Svc, x_pv_cloud_key: str | None = Header(default=None)) -> dict[str, Any]:
    params = dict(body.params)
    if x_pv_cloud_key:
        params["cloud_key"] = x_pv_cloud_key  # in-memory only; never written to the task row
    try:
        tid = await svc.orchestrator.submit(TaskRequest(body.kind, body.request, body.project_id, params))
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"id": tid}


@router.get("/tasks")
def list_tasks(svc: Svc, limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:
    return svc.orchestrator.list_tasks(limit)


@router.get("/tasks/{task_id}")
def get_task(task_id: int, svc: Svc) -> dict[str, Any]:
    try:
        return svc.orchestrator.get(task_id)
    except LookupError as exc:
        raise HTTPException(404, "task not found") from exc


@router.get("/tasks/{task_id}/report", response_class=PlainTextResponse)
def task_report(task_id: int, svc: Svc) -> str:
    try:
        return svc.orchestrator.report_markdown(task_id)
    except LookupError as exc:
        raise HTTPException(404, "task not found") from exc


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: int, svc: Svc) -> dict[str, bool]:
    cancelled = svc.orchestrator.cancel(task_id)
    if cancelled:
        svc.audit.record("user", "task.cancel", resource_type="task", resource_id=task_id)
    return {"cancelled": cancelled}


# ------------------------------------------------------------------ agents
@router.get("/agents")
def list_agents(svc: Svc) -> list[dict[str, Any]]:
    return svc.agents.describe()


@router.patch("/agents/{key}")
def update_agent(key: str, body: AgentUpdate, svc: Svc) -> dict[str, Any]:
    if key not in {a["key"] for a in svc.agents.describe()}:
        raise HTTPException(404, "agent not found")
    if key == "orchestrator" and not body.enabled:
        raise HTTPException(409, "the orchestrator cannot be disabled")
    disabled = set(svc.config.config.agents.disabled)
    (disabled.discard if body.enabled else disabled.add)(key)
    svc.config.update({"agents": {"disabled": sorted(disabled)}})
    svc.audit.record("user", "agent.update", resource_type="agent", resource_id=key, detail={"enabled": body.enabled})
    return next(a for a in svc.agents.describe() if a["key"] == key)


# ------------------------------------------------------------------ testing
@router.post("/testing/run", status_code=202)
async def run_tests(body: TestRunIn, svc: Svc) -> dict[str, Any]:
    bad = [a for a in (body.agents or []) if a not in TESTING_AGENT_KEYS]
    if bad:
        raise HTTPException(422, f"unknown testing agent(s): {', '.join(bad)}")
    try:
        tid = await svc.orchestrator.submit(
            TaskRequest("test", "Run tests", body.project_id, {"agents": body.agents, "fix": body.fix, **body.params})
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"id": tid}


def _session(ts: TestingSession) -> dict[str, Any]:
    return {
        "id": ts.id,
        "project_id": ts.project_id,
        "task_id": ts.task_id,
        "agent_key": ts.agent_key,
        "status": ts.status,
        "command": ts.command,
        "summary": ts.summary,
        "coverage_percent": ts.coverage_percent,
        "started_at": ts.started_at.isoformat(),
        "finished_at": ts.finished_at.isoformat() if ts.finished_at else None,
    }


@router.get("/testing/sessions")
def testing_sessions(
    db: Db, project_id: int | None = None, agent: str | None = None, limit: int = Query(50, ge=1, le=300)
) -> list[dict[str, Any]]:
    q = select(TestingSession).order_by(TestingSession.id.desc()).limit(limit)
    if project_id:
        q = q.where(TestingSession.project_id == project_id)
    if agent:
        q = q.where(TestingSession.agent_key == agent)
    return [_session(t) for t in db.execute(q).scalars()]


@router.get("/testing/sessions/{session_id}")
def testing_session(session_id: int, db: Db) -> dict[str, Any]:
    ts = db.get(TestingSession, session_id)
    if ts is None:
        raise HTTPException(404, "testing session not found")
    out = _session(ts)
    out["results"] = [
        {
            "name": r.name,
            "status": r.status,
            "description": r.description,
            "expected": r.expected,
            "actual": r.actual,
            "error_details": r.error_details,
            "correction_applied": r.correction_applied,
            "retest_status": r.retest_status,
            "duration_ms": r.duration_ms,
            "file_path": r.file_path,
        }
        for r in ts.results
    ]
    return out


@router.get("/testing/summary")
def testing_summary(db: Db, project_id: int | None = None) -> dict[str, Any]:
    agents = []
    for key in TESTING_AGENT_KEYS:
        q = select(TestingSession).where(TestingSession.agent_key == key).order_by(TestingSession.id.desc()).limit(1)
        if project_id:
            q = q.where(TestingSession.project_id == project_id)
        last = db.execute(q).scalars().first()
        agents.append({"agent": key, "last": _session(last) if last else None})
    failing = (
        db.scalar(
            select(func.count())
            .select_from(TestResult)
            .join(TestingSession)
            .where(
                TestResult.status.in_(("failed", "error")),
                *([TestingSession.project_id == project_id] if project_id else []),
            )
        )
        or 0
    )
    return {
        "agents": agents,
        "failing_results_total": failing,
        "projects": db.scalar(select(func.count()).select_from(Project)) or 0,
    }


# ------------------------------------------------------------------ approvals
@router.get("/approvals")
def approvals(svc: Svc, pending_only: bool = False) -> list[dict[str, Any]]:
    return svc.approvals.pending() if pending_only else svc.approvals.list()


@router.post("/approvals/{approval_id}/decide")
def decide(approval_id: int, body: ApprovalDecision, svc: Svc) -> dict[str, Any]:
    try:
        return svc.approvals.decide(approval_id, body.approve, body.note)
    except LookupError as exc:
        raise HTTPException(404, "approval not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


# ------------------------------------------------------------------ recovery
@router.get("/recovery-points")
def recovery_points(svc: Svc, project_id: int | None = None) -> list[dict[str, Any]]:
    return svc.recovery.list(project_id)


@router.post("/recovery-points/{point_id}/restore")
def restore(point_id: int, db: Db, svc: Svc, confirm: bool = False) -> dict[str, Any]:
    if not confirm:
        raise HTTPException(400, "destructive operation: pass confirm=true")
    pts = {p["id"]: p for p in svc.recovery.list(limit=1000)}
    if point_id not in pts:
        raise HTTPException(404, "recovery point not found")
    project = db.get(Project, pts[point_id]["project_id"])
    if project is None:
        raise HTTPException(404, "project not found")
    from pathlib import Path

    try:
        return svc.recovery.restore(point_id, Path(project.root_path))
    except RecoveryError as exc:
        raise HTTPException(409, str(exc)) from exc


# ------------------------------------------------------------------ backup & restore
@router.get("/backups")
def list_backups(svc: Svc) -> list[dict[str, Any]]:
    return svc.backups.list()


@router.post("/backups", status_code=201)
def create_backup(body: BackupCreate, svc: Svc) -> dict[str, Any]:
    try:
        return svc.backups.create(body.passphrase, body.label)
    except BackupError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/backups/verify")
def verify_backup(body: BackupRestore, svc: Svc) -> dict[str, Any]:
    try:
        return svc.backups.verify(body.name, body.passphrase)
    except BackupError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/backups/restore")
def restore_backup(body: BackupRestore, svc: Svc) -> dict[str, Any]:
    if not body.confirm:
        raise HTTPException(400, "destructive operation: pass confirm=true")
    if any(not t.done() for t in svc.orchestrator._running.values()):
        raise HTTPException(409, "tasks are running; wait for them to finish before restoring")
    try:
        return svc.backups.restore(body.name, body.passphrase)
    except BackupError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete("/backups/{name}", status_code=204)
def delete_backup(name: str, svc: Svc, confirm: bool = False) -> None:
    if not confirm:
        raise HTTPException(400, "destructive operation: pass confirm=true")
    try:
        svc.backups.delete(name)
    except BackupError as exc:
        raise HTTPException(404, str(exc)) from exc


# ------------------------------------------------------------------ storage
@router.get("/storage")
def storage(svc: Svc) -> dict[str, Any]:
    return svc.storage.usage()


@router.post("/storage/gc")
def storage_gc(svc: Svc) -> dict[str, Any]:
    return svc.storage.gc_blobs()
