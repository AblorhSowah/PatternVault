"""Project registry endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.api.deps import Authed, Db, Svc
from app.api.schemas import ProjectCreate, ProjectOut, ProjectUpdate
from app.models import Project

router = APIRouter(prefix="/projects", tags=["projects"], dependencies=[Authed])


@router.get("", response_model=list[ProjectOut])
def list_projects(db: Db) -> list[Project]:
    return list(db.execute(select(Project).order_by(Project.name)).scalars())


@router.post("", response_model=ProjectOut, status_code=201)
def register_project(body: ProjectCreate, db: Db, svc: Svc) -> Project:
    root = Path(body.root_path).expanduser()
    if not root.is_dir():
        raise HTTPException(422, "root_path is not an existing directory")
    root = root.resolve()
    home = svc.paths.home.resolve()
    if root == home or home in root.parents:
        raise HTTPException(422, "the PatternVault data directory cannot be registered as a project")
    existing = db.execute(select(Project).where(Project.root_path == str(root))).scalar_one_or_none()
    if existing:
        return existing
    project = Project(name=body.name or root.name, root_path=str(root))
    db.add(project)
    db.commit()  # release the write lock before auditing (the audit log uses its own connection)
    svc.audit.record(
        "user", "project.register", resource_type="project", resource_id=project.id, detail={"root": str(root)}
    )
    return project


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: int, db: Db) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "project not found")
    return project


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(project_id: int, body: ProjectUpdate, db: Db, svc: Svc) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "project not found")
    changes = body.model_dump(exclude_unset=True)
    for k, v in changes.items():
        setattr(project, k, v)
    db.commit()
    svc.audit.record("user", "project.update", resource_type="project", resource_id=project_id, detail=changes)
    return project


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: int, db: Db, svc: Svc, confirm: bool = False) -> None:
    """Deletes the project's captured knowledge records (never the files on disk). Needs ``confirm=true``."""
    if not confirm:
        raise HTTPException(400, "destructive operation: pass confirm=true")
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "project not found")
    svc.audit.record(
        "user", "project.delete", resource_type="project", resource_id=project_id, detail={"root": project.root_path}
    )
    db.delete(project)
