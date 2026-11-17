"""Storage accounting, configurable limits and blob garbage collection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.models import DevSession, FileSnapshot

MB = 1024 * 1024


class StorageLimitExceeded(RuntimeError):
    pass


def _dir_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0


class StorageMonitor:
    def __init__(self, svc: Any) -> None:
        self.svc = svc

    def usage(self) -> dict[str, Any]:
        svc, cfg = self.svc, self.svc.config.config.storage
        db = sum(
            Path(str(svc.paths.db) + s).stat().st_size
            for s in ("", "-wal", "-shm")
            if Path(str(svc.paths.db) + s).exists()
        )
        blobs = _dir_bytes(svc.paths.blobs)
        out = {
            "db_bytes": db,
            "blob_bytes": blobs,
            "vector_bytes": svc.paths.vectors.stat().st_size if svc.paths.vectors.exists() else 0,
            "backup_bytes": _dir_bytes(svc.paths.backups),
            "recovery_bytes": _dir_bytes(svc.paths.recovery),
            "limits": {"max_db_mb": cfg.max_db_mb, "max_blobs_mb": cfg.max_blobs_mb},
        }
        out["over_db_limit"] = db > cfg.max_db_mb * MB
        out["over_blob_limit"] = blobs > cfg.max_blobs_mb * MB
        return out

    def ensure_capture_allowed(self) -> None:
        u = self.usage()
        if u["over_blob_limit"]:
            raise StorageLimitExceeded(
                f"knowledge blob storage is over its limit ({u['blob_bytes'] // MB} MB > {u['limits']['max_blobs_mb']} MB): "
                "delete old sessions, run garbage collection, or raise the limit in Settings"
            )
        if u["over_db_limit"]:
            raise StorageLimitExceeded(
                f"the knowledge database is over its limit ({u['db_bytes'] // MB} MB > {u['limits']['max_db_mb']} MB)"
            )

    def gc_blobs(self) -> dict[str, Any]:
        """Delete blobs no snapshot or session baseline refers to (e.g. after deleting sessions/projects)."""
        svc = self.svc
        with svc.session_factory() as db:
            referenced = {h for (h,) in db.execute(select(FileSnapshot.content_hash)).all() if h}
            for status, meta in db.execute(select(DevSession.status, DevSession.meta)).all():
                if not (isinstance(meta, dict) and meta.get("baseline_blob")):
                    continue
                referenced.add(meta["baseline_blob"])
                if status == "active":
                    # an open capture window still needs every "before" blob listed in its baseline manifest
                    manifest = svc.blobs.get(meta["baseline_blob"])
                    if manifest:
                        for entry in json.loads(manifest).get("files", {}).values():
                            if entry[1]:
                                referenced.add(entry[1])
        freed = removed = 0
        for p in list(svc.paths.blobs.rglob("*")):
            if p.is_file() and not p.name.startswith(".") and p.name not in referenced:
                freed += p.stat().st_size
                removed += 1
                p.unlink()
        for d in sorted((p for p in svc.paths.blobs.rglob("*") if p.is_dir()), reverse=True):
            if not any(d.iterdir()):
                d.rmdir()
        svc.audit.record(
            "user", "storage.gc", resource_type="storage", detail={"removed": removed, "freed_bytes": freed}
        )
        return {"removed": removed, "freed_bytes": freed, "kept": len(referenced)}
