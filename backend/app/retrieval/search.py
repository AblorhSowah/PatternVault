"""Hybrid retrieval: FTS5 keyword score + vector similarity, re-ranked by trust and project/framework fit."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.knowledge import fts
from app.models import KnowledgePattern, Project
from app.retrieval.indexer import EmbedderProvider
from app.retrieval.vector_store import VectorStore

TRUST = {"verified": 1.0, "tested": 0.85, "approved": 0.7, "observed": 0.5, "generated": 0.4, "deprecated": 0.1}
W_KEYWORD, W_VECTOR = 0.55, 0.45


def major(version: str | None) -> int | None:
    m = re.match(r"\s*[v^~>=<]*\s*(\d+)", version or "")
    return int(m.group(1)) if m else None


@dataclass
class SearchQuery:
    text: str
    project_id: int | None = None
    language: str | None = None
    framework: str | None = None
    framework_version: str | None = None
    categories: list[str] | None = None
    statuses: list[str] | None = None
    include_deprecated: bool = False
    cross_project: bool = True
    top_k: int = 10


@dataclass
class SearchHit:
    pattern_id: int
    score: float
    relevance: float
    keyword: float
    semantic: float
    trust: float
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pattern: dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchResponse:
    hits: list[SearchHit]
    embedder: str
    notes: list[str] = field(default_factory=list)


class KnowledgeSearch:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        store: VectorStore,
        embedders: EmbedderProvider,
        pattern_service: Any,
        config_getter: Any,
    ) -> None:
        self._factory = session_factory
        self._store = store
        self._embedders = embedders
        self._patterns = pattern_service
        self._cfg = config_getter

    def search(self, q: SearchQuery) -> SearchResponse:
        notes: list[str] = []
        emb = self._embedders.current()
        with self._factory() as db:
            kw_raw = dict(fts.search(db, q.text, limit=200))
        kw_max = max(kw_raw.values(), default=0.0)
        keyword = {pid: (s / kw_max if kw_max > 0 else 0.0) for pid, s in kw_raw.items()}
        semantic: dict[int, float] = {}
        if q.text.strip():
            vec = emb.embed([q.text])[0]
            hits = self._store.search(vec, 200, emb.name)
            if not hits and self._store.count() > 0 and self._store.count(emb.name) == 0:
                notes.append(f"vectors were built with a different embedding model than '{emb.name}': run reindex")
            for key, score, meta in hits:
                if score >= emb.min_similarity:
                    semantic[int(meta.get("pattern_id", key.split(":")[1]))] = score
        candidate_ids = set(keyword) | set(semantic)
        if not candidate_ids:
            return SearchResponse([], emb.name, notes)
        allow_cross = q.cross_project and self._cfg().privacy.allow_cross_project_retrieval
        with self._factory() as db:
            rows = {
                p.id: p
                for p in db.execute(select(KnowledgePattern).where(KnowledgePattern.id.in_(candidate_ids))).scalars()
            }
            shareable = {pr.id: pr.share_knowledge for pr in db.execute(select(Project)).scalars()}
            hits_out: list[SearchHit] = []
            for pid in candidate_ids:
                p = rows.get(pid)
                if p is None:
                    continue
                if p.status == "deprecated" and not q.include_deprecated:
                    continue
                if q.statuses and p.status not in q.statuses:
                    continue
                if q.categories and p.category not in q.categories:
                    continue
                same_project = q.project_id is not None and p.project_id == q.project_id
                if q.project_id is not None and not same_project and p.project_id is not None:
                    if not allow_cross:
                        continue
                    if not shareable.get(p.project_id, True):
                        continue
                hits_out.append(self._score(p, q, keyword.get(pid, 0.0), semantic.get(pid, 0.0), same_project))
            hits_out.sort(key=lambda h: -h.score)
            top = hits_out[: q.top_k]
            for h in top:
                h.pattern = self._patterns._to_dict(db, rows[h.pattern_id], False)
        if top and not any(h.pattern["status"] in ("verified", "tested") for h in top):
            notes.append("none of the top results is tested or verified")
        return SearchResponse(top, emb.name, notes)

    @staticmethod
    def _score(p: KnowledgePattern, q: SearchQuery, kw: float, sem: float, same_project: bool) -> SearchHit:
        relevance = W_KEYWORD * kw + W_VECTOR * sem
        trust = TRUST[p.status]
        score = relevance * (0.5 + 0.5 * trust)
        reasons = [f"keyword {kw:.2f}", f"semantic {sem:.2f}", f"status {p.status}"]
        warnings: list[str] = []
        if q.language:
            if p.language == q.language:
                score += 0.08
                reasons.append(f"language {p.language} matches")
            elif p.language:
                score -= 0.12
                warnings.append(f"written for {p.language}, project uses {q.language}")
        if q.framework and p.framework:
            if p.framework.lower() == q.framework.lower():
                score += 0.08
                reasons.append(f"framework {p.framework} matches")
                pm, qm = major(p.framework_version), major(q.framework_version)
                if pm is not None and qm is not None:
                    if pm == qm:
                        score += 0.04
                        reasons.append(f"same major version ({qm})")
                    else:
                        score -= min(0.15, 0.05 * abs(pm - qm))
                        warnings.append(f"learned on {p.framework} {pm}, project uses {qm}")
            else:
                score -= 0.08
                warnings.append(f"learned on {p.framework}, project uses {q.framework}")
        if same_project:
            score += 0.04
            reasons.append("from this project")
        return SearchHit(
            p.id, round(score, 4), round(relevance, 4), round(kw, 3), round(sem, 3), trust, reasons, warnings
        )
