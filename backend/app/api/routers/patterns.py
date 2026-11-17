"""Knowledge pattern endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import Authed, Svc
from app.api.schemas import FeedbackIn, PatternUpdate, RelateIn
from app.knowledge.lifecycle import LifecycleError
from app.knowledge.service import PatternNotFound

router = APIRouter(prefix="/patterns", tags=["patterns"], dependencies=[Authed])


def _nf(exc: PatternNotFound) -> HTTPException:
    return HTTPException(404, "pattern not found")


@router.get("")
def list_patterns(
    svc: Svc,
    status: str | None = None,
    category: str | None = None,
    language: str | None = None,
    framework: str | None = None,
    project_id: int | None = None,
    q: str | None = Query(None, max_length=500),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    return svc.patterns.list_patterns(
        status=status,
        category=category,
        language=language,
        framework=framework,
        project_id=project_id,
        q=q,
        limit=limit,
        offset=offset,
    )


@router.get("/facets")
def facets(svc: Svc) -> dict[str, Any]:
    return svc.patterns.facets()


@router.get("/{pattern_id}")
def get_pattern(pattern_id: int, svc: Svc) -> dict[str, Any]:
    try:
        return svc.patterns.get(pattern_id)
    except PatternNotFound as exc:
        raise _nf(exc) from exc


@router.get("/{pattern_id}/versions")
def versions(pattern_id: int, svc: Svc) -> list[dict[str, Any]]:
    try:
        return svc.patterns.versions(pattern_id)
    except PatternNotFound as exc:
        raise _nf(exc) from exc


@router.patch("/{pattern_id}")
def update(pattern_id: int, body: PatternUpdate, svc: Svc) -> dict[str, Any]:
    try:
        return svc.patterns.update_metadata(pattern_id, **body.model_dump(exclude_unset=True))
    except PatternNotFound as exc:
        raise _nf(exc) from exc


@router.post("/{pattern_id}/approve")
def approve(pattern_id: int, svc: Svc, comment: str = "") -> dict[str, Any]:
    try:
        return svc.patterns.approve(pattern_id, comment)
    except PatternNotFound as exc:
        raise _nf(exc) from exc
    except LifecycleError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{pattern_id}/deprecate")
def deprecate(pattern_id: int, svc: Svc, comment: str = "") -> dict[str, Any]:
    try:
        return svc.patterns.deprecate(pattern_id, comment)
    except PatternNotFound as exc:
        raise _nf(exc) from exc


@router.post("/{pattern_id}/restore")
def restore(pattern_id: int, svc: Svc) -> dict[str, Any]:
    try:
        return svc.patterns.restore(pattern_id)
    except PatternNotFound as exc:
        raise _nf(exc) from exc
    except LifecycleError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{pattern_id}/feedback")
def feedback(pattern_id: int, body: FeedbackIn, svc: Svc) -> dict[str, Any]:
    try:
        return svc.patterns.add_feedback(
            pattern_id, rating=body.rating, comment=body.comment, action=body.action, correction=body.correction
        )
    except PatternNotFound as exc:
        raise _nf(exc) from exc


@router.post("/{pattern_id}/relations", status_code=201)
def relate(pattern_id: int, body: RelateIn, svc: Svc) -> dict[str, bool]:
    try:
        svc.patterns.relate(pattern_id, body.target_id, body.relation, body.score, body.note)
    except PatternNotFound as exc:
        raise _nf(exc) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"ok": True}


@router.delete("/{pattern_id}", status_code=204)
def delete(pattern_id: int, svc: Svc, confirm: bool = False) -> None:
    if not confirm:
        raise HTTPException(400, "destructive operation: pass confirm=true")
    try:
        svc.patterns.delete(pattern_id)
    except PatternNotFound as exc:
        raise _nf(exc) from exc
