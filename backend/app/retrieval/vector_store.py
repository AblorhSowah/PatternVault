"""Vector stores (ADR-004): SQLite-backed default, ChromaDB optional."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any, Protocol

import numpy as np


class VectorStore(Protocol):
    def upsert(self, key: str, vec: np.ndarray, model: str, meta: dict[str, Any]) -> None: ...
    def delete(self, key: str) -> None: ...
    def search(self, vec: np.ndarray, top_k: int, model: str) -> list[tuple[str, float, dict[str, Any]]]: ...
    def count(self, model: str | None = None) -> int: ...
    def clear(self, model: str | None = None) -> None: ...
    def models(self) -> dict[str, int]: ...
    def keys(self, model: str) -> set[str]: ...


class SqliteVectorStore:
    """float32 blobs in their own SQLite file; cosine search via a cached NumPy matrix (good to ~10^5 vectors)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[list[str], np.ndarray, list[dict[str, Any]]]] = {}
        with closing(self._conn()) as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, model TEXT NOT NULL, dim INTEGER NOT NULL, "
                "vec BLOB NOT NULL, meta TEXT NOT NULL DEFAULT '{}')"
            )
            c.execute("CREATE INDEX IF NOT EXISTS ix_vectors_model ON vectors(model)")
            c.commit()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=15)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def upsert(self, key: str, vec: np.ndarray, model: str, meta: dict[str, Any]) -> None:
        v = np.asarray(vec, dtype=np.float32).reshape(-1)
        with self._lock, closing(self._conn()) as c:
            c.execute(
                "INSERT INTO vectors(key, model, dim, vec, meta) VALUES (?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET model=excluded.model, dim=excluded.dim, vec=excluded.vec, meta=excluded.meta",
                (key, model, int(v.shape[0]), v.tobytes(), json.dumps(meta)),
            )
            c.commit()
            self._cache.clear()

    def delete(self, key: str) -> None:
        with self._lock, closing(self._conn()) as c:
            c.execute("DELETE FROM vectors WHERE key = ?", (key,))
            c.commit()
            self._cache.clear()

    def _load(self, model: str) -> tuple[list[str], np.ndarray, list[dict[str, Any]]]:
        if model in self._cache:
            return self._cache[model]
        with closing(self._conn()) as c:
            rows = c.execute("SELECT key, dim, vec, meta FROM vectors WHERE model = ?", (model,)).fetchall()
        keys = [r[0] for r in rows]
        metas = [json.loads(r[3]) for r in rows]
        mat = np.vstack([np.frombuffer(r[2], dtype=np.float32) for r in rows]) if rows else np.zeros((0, 0), np.float32)
        self._cache[model] = (keys, mat, metas)
        return self._cache[model]

    def search(self, vec: np.ndarray, top_k: int, model: str) -> list[tuple[str, float, dict[str, Any]]]:
        with self._lock:
            keys, mat, metas = self._load(model)
        if not keys:
            return []
        q = np.asarray(vec, dtype=np.float32).reshape(-1)
        if mat.shape[1] != q.shape[0]:
            return []  # dimension mismatch = vectors from another model; caller should reindex
        scores = mat @ q
        order = np.argsort(-scores)[:top_k]
        return [(keys[i], float(scores[i]), metas[i]) for i in order]

    def count(self, model: str | None = None) -> int:
        with closing(self._conn()) as c:
            if model:
                return int(c.execute("SELECT COUNT(*) FROM vectors WHERE model = ?", (model,)).fetchone()[0])
            return int(c.execute("SELECT COUNT(*) FROM vectors").fetchone()[0])

    def clear(self, model: str | None = None) -> None:
        with self._lock, closing(self._conn()) as c:
            if model:
                c.execute("DELETE FROM vectors WHERE model = ?", (model,))
            else:
                c.execute("DELETE FROM vectors")
            c.commit()
            self._cache.clear()

    def keys(self, model: str) -> set[str]:
        with closing(self._conn()) as c:
            return {r[0] for r in c.execute("SELECT key FROM vectors WHERE model = ?", (model,)).fetchall()}

    def models(self) -> dict[str, int]:
        with closing(self._conn()) as c:
            return dict(c.execute("SELECT model, COUNT(*) FROM vectors GROUP BY model").fetchall())


class ChromaVectorStore:
    """ChromaDB adapter (optional extra: ``pip install patternvault[chroma]``)."""

    def __init__(self, path: Path) -> None:
        try:
            import chromadb
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError("ChromaDB is not installed: pip install 'patternvault[chroma]'") from exc
        self._client = chromadb.PersistentClient(path=str(path))

    def _col(self, model: str) -> Any:
        safe = "pv_" + "".join(ch if ch.isalnum() else "_" for ch in model)[:55]
        return self._client.get_or_create_collection(safe, metadata={"hnsw:space": "cosine", "model": model})

    def upsert(self, key: str, vec: np.ndarray, model: str, meta: dict[str, Any]) -> None:
        flat = {
            k: (v if isinstance(v, str | int | float | bool) else json.dumps(v))
            for k, v in meta.items()
            if v is not None
        }
        self._col(model).upsert(
            ids=[key], embeddings=[np.asarray(vec, dtype=np.float32).tolist()], metadatas=[flat or {"_": 1}]
        )

    def delete(self, key: str) -> None:
        for col in self._client.list_collections():
            name = col if isinstance(col, str) else col.name
            self._client.get_collection(name).delete(ids=[key])

    def search(self, vec: np.ndarray, top_k: int, model: str) -> list[tuple[str, float, dict[str, Any]]]:
        col = self._col(model)
        if col.count() == 0:
            return []
        res = col.query(
            query_embeddings=[np.asarray(vec, dtype=np.float32).tolist()], n_results=min(top_k, col.count())
        )
        return [
            (i, 1.0 - float(d), dict(m or {}))
            for i, d, m in zip(res["ids"][0], res["distances"][0], res["metadatas"][0], strict=True)
        ]

    def count(self, model: str | None = None) -> int:
        if model:
            return int(self._col(model).count())
        return sum(
            int(self._client.get_collection(c if isinstance(c, str) else c.name).count())
            for c in self._client.list_collections()
        )

    def clear(self, model: str | None = None) -> None:
        for col in self._client.list_collections():
            name = col if isinstance(col, str) else col.name
            self._client.delete_collection(name)

    def keys(self, model: str) -> set[str]:
        return set(self._col(model).get()["ids"])

    def models(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for col in self._client.list_collections():
            c = self._client.get_collection(col if isinstance(col, str) else col.name)
            out[(c.metadata or {}).get("model", c.name)] = int(c.count())
        return out
