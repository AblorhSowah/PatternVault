"""Keeps the vector index in sync with the pattern store and picks the active embedder."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import EmbeddingIndexEntry, KnowledgePattern
from app.models.base import utcnow
from app.retrieval.embeddings import Embedder, EmbeddingError, HashingEmbedder, OllamaEmbedder
from app.retrieval.vector_store import VectorStore

log = logging.getLogger("patternvault")


def pattern_text(p: KnowledgePattern) -> str:
    """Text that represents a pattern for semantic search: metadata + file paths + symbols + the original request."""
    v = p.current_version
    parts = [p.title, p.summary, p.category, p.language or "", p.framework or "", " ".join(p.tags or [])]
    if v:
        c = v.content
        parts.append(str(c.get("request", "")))
        for f in c.get("files", []):
            parts.append(f.get("path", ""))
            parts.extend(s.get("name", "") for s in f.get("symbols", []))
            parts.extend(f"{r['method']} {r['path']}" for r in f.get("routes", []))
            parts.extend(f.get("imports", [])[:15])
        parts.extend(d.get("name", "") for d in c.get("dependencies", []))
    return "\n".join(str(x) for x in parts if x)


class EmbedderProvider:
    """Chooses Ollama embeddings when configured *and* reachable, else the offline hashing embedder."""

    def __init__(self, config_getter: Callable[[], Any]) -> None:
        self._cfg = config_getter
        self._hash = HashingEmbedder()
        self._lock = threading.Lock()
        self._reach: tuple[float, bool] = (0.0, False)

    def _ollama_reachable(self, base_url: str) -> bool:
        with self._lock:
            ts, ok = self._reach
            if time.monotonic() - ts < 30:
                return ok
        try:
            ok = httpx.get(f"{base_url.rstrip('/')}/api/version", timeout=1.5).status_code == 200
        except httpx.HTTPError:
            ok = False
        with self._lock:
            self._reach = (time.monotonic(), ok)
        return ok

    def current(self) -> Embedder:
        lm = self._cfg().local_model
        if lm.embedding_model and self._ollama_reachable(lm.base_url):
            return OllamaEmbedder(lm.base_url, lm.embedding_model)
        return self._hash

    def invalidate(self) -> None:
        with self._lock:
            self._reach = (0.0, False)


class PatternIndexer:
    def __init__(self, session_factory: sessionmaker[Session], store: VectorStore, embedders: EmbedderProvider) -> None:
        self._factory = session_factory
        self.store = store
        self.embedders = embedders

    def on_pattern_event(self, event: str, pattern_id: int) -> None:
        if event == "deleted":
            self.remove(pattern_id)
        else:
            self.index(pattern_id)

    def index(self, pattern_id: int) -> bool:
        emb = self.embedders.current()
        with self._factory() as db:
            p = db.get(KnowledgePattern, pattern_id)
            if p is None:
                return False
            text = pattern_text(p)
            meta = {
                "pattern_id": p.id,
                "status": p.status,
                "category": p.category,
                "language": p.language,
                "framework": p.framework,
                "project_id": p.project_id,
            }
            version_id = p.current_version_id or 0
        try:
            vec = emb.embed([text])[0]
        except EmbeddingError as exc:
            log.warning("embedding failed (%s); falling back to hashing embedder", exc)
            self.embedders.invalidate()
            emb = self.embedders._hash
            vec = emb.embed([text])[0]
        self.store.upsert(f"p:{pattern_id}", vec, emb.name, meta)
        with self._factory() as db:
            entry = db.execute(
                select(EmbeddingIndexEntry).where(EmbeddingIndexEntry.pattern_id == pattern_id)
            ).scalar_one_or_none()
            if entry is None:
                db.add(
                    EmbeddingIndexEntry(
                        pattern_id=pattern_id, version_id=version_id, model=emb.name, dim=int(vec.shape[0])
                    )
                )
            else:
                # drop the stale vector made by a previous model so each pattern has exactly one vector
                if entry.model != emb.name:
                    self.store.delete(f"p:{pattern_id}")
                    self.store.upsert(f"p:{pattern_id}", vec, emb.name, meta)
                entry.version_id, entry.model, entry.dim, entry.indexed_at = (
                    version_id,
                    emb.name,
                    int(vec.shape[0]),
                    utcnow(),
                )
            db.commit()
        return True

    def remove(self, pattern_id: int) -> None:
        self.store.delete(f"p:{pattern_id}")
        with self._factory() as db:
            entry = db.execute(
                select(EmbeddingIndexEntry).where(EmbeddingIndexEntry.pattern_id == pattern_id)
            ).scalar_one_or_none()
            if entry:
                db.delete(entry)
                db.commit()

    def reindex_all(self) -> dict[str, Any]:
        with self._factory() as db:
            ids = [i for (i,) in db.execute(select(KnowledgePattern.id)).all()]
        ok = sum(1 for i in ids if self.index(i))
        return {"indexed": ok, "total": len(ids), "model": self.embedders.current().name}

    def stale(self) -> list[int]:
        """Patterns whose vector is missing, or was produced by a different model/version than the active one."""
        emb = self.embedders.current()
        present = self.store.keys(emb.name)
        with self._factory() as db:
            rows = db.execute(
                select(
                    KnowledgePattern.id,
                    KnowledgePattern.current_version_id,
                    EmbeddingIndexEntry.model,
                    EmbeddingIndexEntry.version_id,
                ).outerjoin(EmbeddingIndexEntry, EmbeddingIndexEntry.pattern_id == KnowledgePattern.id)
            ).all()
        return [pid for pid, cur_v, m, v in rows if m != emb.name or v != (cur_v or 0) or f"p:{pid}" not in present]
