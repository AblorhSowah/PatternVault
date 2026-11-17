"""Recovery points: exact pre-change copies of files so any agent modification can be rolled back.

Unlike capture blobs (redacted), recovery data holds the user's RAW bytes: it must restore files exactly. It lives
in ``<home>/recovery`` (directory mode 0700, files 0600), is limited to files an agent is about to change, and is
pruned by :meth:`RecoveryManager.expire`.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.models import RecoveryPoint
from app.models.base import utcnow


class RecoveryError(RuntimeError):
    pass


class RecoveryManager:
    def __init__(self, root: Path, session_factory: sessionmaker[Session], audit: Any) -> None:
        self.root = root
        self._factory = session_factory
        self._audit = audit
        self.root.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.root, 0o700)

    def _data_dir(self, point_id: int) -> Path:
        return self.root / str(point_id)

    def create(
        self, project_id: int, project_root: Path, rel_paths: list[str], *, task_id: int | None = None, label: str = ""
    ) -> int:
        project_root = project_root.resolve()
        with self._factory() as db:
            point = RecoveryPoint(project_id=project_id, task_id=task_id, label=label, files=[], status="active")
            db.add(point)
            db.commit()
            pid = point.id
        data = self._data_dir(pid)
        data.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(data, 0o700)
        entries: list[dict[str, Any]] = []
        for rel in dict.fromkeys(rel_paths):
            full = (project_root / rel).resolve()
            try:
                full.relative_to(project_root)
            except ValueError as exc:
                raise RecoveryError(f"refusing to snapshot outside the project: {rel}") from exc
            if full.is_file() and not full.is_symlink():
                raw = full.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                blob = data / digest
                if not blob.exists():
                    blob.write_bytes(raw)
                    with contextlib.suppress(OSError):
                        os.chmod(blob, 0o600)
                entries.append({"path": rel, "hash": digest, "mode": full.stat().st_mode & 0o777})
            else:
                entries.append({"path": rel, "hash": None, "mode": None})  # did not exist
        with self._factory() as db:
            db.get(RecoveryPoint, pid).files = entries  # type: ignore[union-attr]
            db.commit()
        self._audit.record(
            "system",
            "recovery.create",
            resource_type="recovery_point",
            resource_id=pid,
            detail={"files": len(entries), "task_id": task_id, "label": label},
        )
        return pid

    def restore(self, point_id: int, project_root: Path) -> dict[str, Any]:
        project_root = project_root.resolve()
        with self._factory() as db:
            point = db.get(RecoveryPoint, point_id)
            if point is None:
                raise RecoveryError("recovery point not found")
            if point.status == "expired":
                raise RecoveryError("recovery point has expired")
            entries = list(point.files)
        restored: list[str] = []
        removed: list[str] = []
        for e in entries:
            full = (project_root / e["path"]).resolve()
            try:
                full.relative_to(project_root)
            except ValueError:
                continue
            if e["hash"] is None:
                if full.is_file():
                    full.unlink()
                    removed.append(e["path"])
                    self._prune_empty_dirs(full.parent, project_root)
            else:
                blob = self._data_dir(point_id) / e["hash"]
                if not blob.exists():
                    raise RecoveryError(f"recovery data for {e['path']} is missing")
                full.parent.mkdir(parents=True, exist_ok=True)
                self._atomic_write(full, blob.read_bytes(), e.get("mode"))
                restored.append(e["path"])
        with self._factory() as db:
            point = db.get(RecoveryPoint, point_id)
            if point is not None:
                point.status, point.restored_at = "restored", utcnow()
                db.commit()
        self._audit.record(
            "user",
            "recovery.restore",
            resource_type="recovery_point",
            resource_id=point_id,
            detail={"restored": len(restored), "removed": len(removed)},
        )
        return {"restored": restored, "removed": removed}

    @staticmethod
    def _prune_empty_dirs(path: Path, stop: Path) -> None:
        while path != stop and path.is_dir() and not any(path.iterdir()):
            path.rmdir()
            path = path.parent

    @staticmethod
    def _atomic_write(path: Path, data: bytes, mode: int | None) -> None:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".pv-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            if mode is not None:
                os.chmod(tmp, mode)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def list(self, project_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self._factory() as db:
            q = db.query(RecoveryPoint).order_by(RecoveryPoint.id.desc())
            if project_id:
                q = q.filter(RecoveryPoint.project_id == project_id)
            return [
                {
                    "id": r.id,
                    "project_id": r.project_id,
                    "task_id": r.task_id,
                    "label": r.label,
                    "status": r.status,
                    "files": [f["path"] for f in r.files],
                    "created_at": r.created_at.isoformat(),
                    "restored_at": r.restored_at.isoformat() if r.restored_at else None,
                }
                for r in q.limit(limit)
            ]

    def expire(self, max_age_days: int = 30) -> int:
        cutoff = utcnow() - timedelta(days=max_age_days)
        n = 0
        with self._factory() as db:
            for r in db.query(RecoveryPoint).filter(
                RecoveryPoint.created_at < cutoff, RecoveryPoint.status != "expired"
            ):
                shutil.rmtree(
                    self._data_dir(r.id), ignore_errors=True
                )  # pv-ignore: PV-DESTRUCTIVE-OP (expired recovery data we created)
                r.status = "expired"
                n += 1
            db.commit()
        return n
