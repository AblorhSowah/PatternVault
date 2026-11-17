"""Knowledge quality: duplicates, conflicts, outdated entries and improvement recommendations.

Everything here is *advisory or relational*: it records ``duplicate_of`` / ``conflicts_with`` relationships (each
audited and individually reversible) and returns recommendations. It never edits pattern content, never changes
permissions/configuration/security policy, and never deletes knowledge.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import KnowledgePattern, PatternRelationship, Project
from app.models.base import utcnow
from app.retrieval.indexer import EmbedderProvider
from app.retrieval.search import major
from app.retrieval.vector_store import VectorStore

DUPLICATE_SIMILARITY = 0.9
STALE_DAYS = 180


class QualityAnalyzer:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        store: VectorStore,
        embedders: EmbedderProvider,
        patterns: Any,
        audit: Any,
    ) -> None:
        self._factory = session_factory
        self._store = store
        self._embedders = embedders
        self._patterns = patterns
        self._audit = audit

    def analyze(self, *, apply_relations: bool = True) -> dict[str, Any]:
        emb = self._embedders.current()
        with self._factory() as db:
            pats = list(
                db.execute(
                    select(KnowledgePattern)
                    .where(KnowledgePattern.status != "deprecated")
                    .order_by(KnowledgePattern.id)
                ).scalars()
            )
            by_id = {p.id: p for p in pats}
            newest_major: dict[str, int] = {}
            for pr in db.execute(select(Project)).scalars():
                for f in pr.frameworks or []:
                    mj = major(f.get("version"))
                    if mj is not None:
                        newest_major[f["name"]] = max(newest_major.get(f["name"], 0), mj)
            deps = {
                p.id: {
                    (d["ecosystem"], d["name"])
                    for d in (p.current_version.content.get("dependencies", []) if p.current_version else [])
                    if not d.get("dev")
                }
                for p in pats
            }
        duplicates = self._duplicates(by_id, emb)
        conflicts = self._conflicts(by_id, deps, {(a, b) for a, b, _ in duplicates})
        outdated = self._outdated(pats, newest_major)
        recs = self._recommendations(pats, duplicates, conflicts, outdated)
        if apply_relations:
            for newer, older, score in duplicates:
                self._patterns.relate(
                    newer, older, "duplicate_of", score, "near-identical implementation (quality analysis)"
                )
            for a, b, note in conflicts:
                self._patterns.relate(a, b, "conflicts_with", 0.0, note)
            self._audit.record(
                "agent:knowledge",
                "knowledge.quality.analyze",
                resource_type="knowledge",
                detail={"duplicates": len(duplicates), "conflicts": len(conflicts), "outdated": len(outdated)},
            )
        return {
            "patterns_analyzed": len(pats),
            "embedder": emb.name,
            "duplicates": [{"pattern_id": a, "duplicate_of": b, "similarity": round(s, 3)} for a, b, s in duplicates],
            "conflicts": [{"a": a, "b": b, "reason": n} for a, b, n in conflicts],
            "outdated": outdated,
            "recommendations": recs,
        }

    def _duplicates(self, by_id: dict[int, KnowledgePattern], emb: Any) -> list[tuple[int, int, float]]:
        out: list[tuple[int, int, float]] = []
        seen: set[tuple[int, int]] = set()
        for p in by_id.values():
            hits = self._store.search(emb.embed([self._text(p)])[0], 6, emb.name)
            for key, score, _meta in hits:
                other_id = int(key.split(":")[1])
                o = by_id.get(other_id)
                if o is None or o.id == p.id or score < DUPLICATE_SIMILARITY:
                    continue
                if o.category != p.category or (p.language and o.language and p.language != o.language):
                    continue
                newer, older = (p, o) if p.id > o.id else (o, p)
                if (newer.id, older.id) not in seen:
                    seen.add((newer.id, older.id))
                    out.append((newer.id, older.id, score))
        return out

    @staticmethod
    def _text(p: KnowledgePattern) -> str:
        from app.retrieval.indexer import pattern_text

        return pattern_text(p)

    @staticmethod
    def _conflicts(
        by_id: dict[int, KnowledgePattern], deps: dict[int, set[tuple[str, str]]], duplicate_pairs: set[tuple[int, int]]
    ) -> list[tuple[int, int, str]]:
        out: list[tuple[int, int, str]] = []
        ids = sorted(by_id)
        for i, a in enumerate(ids):
            for b in ids[i + 1 :]:
                pa, pb = by_id[a], by_id[b]
                if pa.category != pb.category or pa.language != pb.language or pa.framework != pb.framework:
                    continue
                if pa.category == "general" or (b, a) in duplicate_pairs or (a, b) in duplicate_pairs:
                    continue
                only_a, only_b = deps[a] - deps[b], deps[b] - deps[a]
                if only_a and only_b:  # each relies on a library the other does not: competing approaches
                    out.append(
                        (
                            a,
                            b,
                            f"uses {', '.join(sorted(n for _, n in only_a))} vs {', '.join(sorted(n for _, n in only_b))}",
                        )
                    )
        return out

    @staticmethod
    def _outdated(pats: list[KnowledgePattern], newest: dict[str, int]) -> list[dict[str, Any]]:
        out = []
        cutoff = utcnow() - timedelta(days=STALE_DAYS)
        for p in pats:
            reasons = []
            pm = major(p.framework_version)
            if p.framework and pm is not None and newest.get(p.framework, 0) > pm:
                reasons.append(f"learned on {p.framework} {pm}; your projects now use {newest[p.framework]}")
            last = p.last_used_at or p.created_at
            if p.status in ("observed", "generated") and last < cutoff:
                reasons.append(f"unverified and unused for over {STALE_DAYS} days")
            if reasons:
                out.append({"pattern_id": p.id, "title": p.title, "reasons": reasons})
        return out

    @staticmethod
    def _recommendations(
        pats: list[KnowledgePattern],
        dups: list[tuple[int, int, float]],
        conflicts: list[tuple[int, int, str]],
        outdated: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        recs: list[dict[str, Any]] = []
        for p in pats:
            if p.status in ("approved", "observed") and p.success_count >= 2:
                recs.append(
                    {
                        "pattern_id": p.id,
                        "kind": "verify",
                        "message": f"'{p.title}' was reused successfully {p.success_count} times but is not verified: run the testing agents.",
                    }
                )
            if p.failure_count > p.success_count and p.use_count >= 3:
                recs.append(
                    {
                        "pattern_id": p.id,
                        "kind": "review",
                        "message": f"'{p.title}' fails more often than it succeeds when reused: review or deprecate.",
                    }
                )
        for newer, older, s in dups:
            recs.append(
                {
                    "pattern_id": newer,
                    "kind": "merge",
                    "message": f"Pattern {newer} is {s:.0%} similar to {older}: keep one and deprecate the other.",
                }
            )
        for a, b, note in conflicts:
            recs.append(
                {
                    "pattern_id": a,
                    "kind": "decide",
                    "message": f"Patterns {a} and {b} are competing approaches ({note}): choose a preferred one.",
                }
            )
        for o in outdated:
            recs.append(
                {
                    "pattern_id": o["pattern_id"],
                    "kind": "refresh",
                    "message": f"'{o['title']}' may be outdated: {o['reasons'][0]}.",
                }
            )
        return recs

    def remove_relation(self, relation_id: int) -> bool:
        with self._factory() as db:
            r = db.get(PatternRelationship, relation_id)
            if r is None:
                return False
            db.delete(r)
            db.commit()
        self._audit.record("user", "knowledge.relation.remove", resource_type="relationship", resource_id=relation_id)
        return True
