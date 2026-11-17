"""Search, context analysis, indexing, quality and export/import."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from app.api.deps import Authed, Svc
from app.api.schemas import ContextRequest, SearchRequest
from app.knowledge.portability import ImportError_
from app.retrieval.search import SearchQuery

router = APIRouter(tags=["knowledge"], dependencies=[Authed])


def _hit(h: Any) -> dict[str, Any]:
    return {
        "pattern_id": h.pattern_id,
        "score": h.score,
        "relevance": h.relevance,
        "keyword": h.keyword,
        "semantic": h.semantic,
        "trust": h.trust,
        "reasons": h.reasons,
        "warnings": h.warnings,
        "pattern": h.pattern,
    }


@router.post("/search")
def search(body: SearchRequest, svc: Svc) -> dict[str, Any]:
    res = svc.search.search(
        SearchQuery(
            body.query,
            body.project_id,
            body.language,
            body.framework,
            body.framework_version,
            body.categories,
            body.statuses,
            body.include_deprecated,
            body.cross_project,
            body.top_k,
        )
    )
    return {"embedder": res.embedder, "notes": res.notes, "hits": [_hit(h) for h in res.hits]}


@router.post("/context")
def context(body: ContextRequest, svc: Svc) -> dict[str, Any]:
    return svc.context.analyze(body.request, body.project_id, body.top_k).to_dict()


@router.get("/knowledge/stats")
def stats(svc: Svc) -> dict[str, Any]:
    emb = svc.embedders.current()
    return {
        "facets": svc.patterns.facets(),
        "embedder": emb.name,
        "vectors": svc.vector_store.models(),
        "stale_vectors": len(svc.indexer.stale()),
        "blob_bytes": svc.blobs.total_bytes(),
    }


@router.post("/knowledge/reindex")
def reindex(svc: Svc) -> dict[str, Any]:
    result = svc.indexer.reindex_all()
    svc.audit.record("user", "knowledge.reindex", resource_type="knowledge", detail=result)
    return result


@router.post("/knowledge/quality")
def quality(svc: Svc, apply: bool = Query(True)) -> dict[str, Any]:
    return svc.quality.analyze(apply_relations=apply)


@router.get("/knowledge/export")
def export(svc: Svc, status: list[str] | None = Query(None)) -> dict[str, Any]:
    return svc.portability.export(statuses=status)


@router.post("/knowledge/import")
def import_(svc: Svc, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return svc.portability.import_(payload)
    except ImportError_ as exc:
        raise HTTPException(422, str(exc)) from exc
