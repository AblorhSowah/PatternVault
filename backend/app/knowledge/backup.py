"""Backup / restore of the knowledge repository.

Format (``.pvbak``): ``PVBK1`` + 16-byte salt + Fernet token (AES-128-CBC + HMAC-SHA256) of a gzip'd tar, key derived from
the passphrase with scrypt (n=2^15, r=8, p=1). An unencrypted variant (``PVBK0`` + raw gzip'd tar) exists for people who
explicitly choose it. Contents: a consistent SQLite snapshot, ``config.json`` and the content-addressed blobs. Vectors are
*not* included (rebuilt by re-indexing); recovery points and logs are never included.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from app.database import create_db_engine, current_revision, head_revision, integrity_check, run_migrations
from app.version import APP_VERSION

MAGIC_ENC = b"PVBK1"
MAGIC_PLAIN = b"PVBK0"
MIN_PASSPHRASE = 8
MAX_ARCHIVE_BYTES = 8 * 2**30
_SAFE_MEMBER = re.compile(r"^(manifest\.json|patternvault\.db|config\.json|blobs/[0-9a-f]{2}/[0-9a-f]{64})$")


class BackupError(RuntimeError):
    pass


def _key(passphrase: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


class BackupService:
    def __init__(self, svc: Any) -> None:
        self.svc = svc

    # ------------------------------------------------------------------ create
    def create(self, passphrase: str | None = None, label: str = "") -> dict[str, Any]:
        svc = self.svc
        if passphrase is not None and len(passphrase) < MIN_PASSPHRASE:
            raise BackupError(f"passphrase must be at least {MIN_PASSPHRASE} characters")
        tmp = Path(tempfile.mkdtemp(prefix="pv-backup-"))
        try:
            snap = tmp / "patternvault.db"
            src = sqlite3.connect(svc.paths.db)
            try:
                dst = sqlite3.connect(snap)
                try:
                    src.backup(dst)  # consistent snapshot, safe while the backend is running
                finally:
                    dst.close()
            finally:
                src.close()
            counts = self._counts(snap)
            manifest = {
                "format": "patternvault-backup",
                "version": 1,
                "created_at": datetime.now(UTC).isoformat(),
                "app_version": APP_VERSION,
                "schema_revision": head_revision(),
                "label": label[:120],
                "counts": counts,
            }
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                self._add_bytes(tar, "manifest.json", json.dumps(manifest, indent=2).encode())
                tar.add(snap, arcname="patternvault.db")
                if svc.paths.config.exists():
                    tar.add(svc.paths.config, arcname="config.json")
                for blob in sorted(svc.paths.blobs.rglob("*")):
                    if blob.is_file() and not blob.name.startswith("."):
                        rel = blob.relative_to(svc.paths.blobs).as_posix()
                        if _SAFE_MEMBER.match(f"blobs/{rel}"):
                            tar.add(blob, arcname=f"blobs/{rel}")
            raw = buf.getvalue()
            if passphrase is None:
                payload = MAGIC_PLAIN + raw
            else:
                salt = os.urandom(16)
                payload = MAGIC_ENC + salt + Fernet(_key(passphrase, salt)).encrypt(raw)
            svc.paths.backups.mkdir(parents=True, exist_ok=True)
            name = f"patternvault-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S-%f')}.pvbak"
            out = svc.paths.backups / name
            fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)  # pv-ignore: PV-DESTRUCTIVE-OP (our own mkdtemp directory)
        svc.audit.record(
            "user",
            "backup.create",
            resource_type="backup",
            resource_id=name,
            detail={"encrypted": passphrase is not None, "bytes": len(payload), **counts},
        )
        return {
            "name": name,
            "path": str(out),
            "bytes": len(payload),
            "encrypted": passphrase is not None,
            "counts": counts,
        }

    @staticmethod
    def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mtime = int(datetime.now(UTC).timestamp())
        tar.addfile(info, io.BytesIO(data))

    _COUNT_SQL = {
        "projects": "SELECT COUNT(*) FROM projects",
        "dev_sessions": "SELECT COUNT(*) FROM dev_sessions",
        "knowledge_patterns": "SELECT COUNT(*) FROM knowledge_patterns",
        "pattern_versions": "SELECT COUNT(*) FROM pattern_versions",
    }

    @classmethod
    def _counts(cls, db: Path) -> dict[str, int]:
        con = sqlite3.connect(db)
        try:
            return {name: int(con.execute(sql).fetchone()[0]) for name, sql in cls._COUNT_SQL.items()}
        finally:
            con.close()

    # ------------------------------------------------------------------ list / delete
    def list(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.svc.paths.backups.glob("*.pvbak"), reverse=True):
            head = p.read_bytes()[:5]
            out.append(
                {
                    "name": p.name,
                    "bytes": p.stat().st_size,
                    "encrypted": head == MAGIC_ENC,
                    "created_at": datetime.fromtimestamp(p.stat().st_mtime, UTC).isoformat(),
                }
            )
        return out

    def delete(self, name: str) -> bool:
        p = self._resolve(name)
        p.unlink()
        self.svc.audit.record("user", "backup.delete", resource_type="backup", resource_id=name)
        return True

    def _resolve(self, name: str) -> Path:
        if not re.fullmatch(r"patternvault-[\w\-]+\.pvbak", name):
            raise BackupError("invalid backup name")
        p = self.svc.paths.backups / name
        if not p.is_file():
            raise BackupError("backup not found")
        return p

    # ------------------------------------------------------------------ restore
    def _open(self, blob: bytes, passphrase: str | None) -> tarfile.TarFile:
        if blob.startswith(MAGIC_ENC):
            if not passphrase:
                raise BackupError("this backup is encrypted: a passphrase is required")
            salt, token = blob[5:21], blob[21:]
            try:
                raw = Fernet(_key(passphrase, salt)).decrypt(token)
            except InvalidToken as exc:
                raise BackupError("wrong passphrase, or the backup file is corrupt or has been tampered with") from exc
        elif blob.startswith(MAGIC_PLAIN):
            raw = blob[5:]
        else:
            raise BackupError("not a PatternVault backup")
        if len(raw) > MAX_ARCHIVE_BYTES:
            raise BackupError("backup is too large")
        try:
            return tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
        except tarfile.TarError as exc:
            raise BackupError(f"backup archive is damaged: {exc}") from exc

    def verify(self, name: str, passphrase: str | None = None) -> dict[str, Any]:
        """Decrypt and validate without changing anything."""
        tar = self._open(self._resolve(name).read_bytes(), passphrase)
        with tar:
            return self._validate_members(tar)

    @staticmethod
    def _validate_members(tar: tarfile.TarFile) -> dict[str, Any]:
        names = []
        for m in tar.getmembers():
            if not m.isfile() or not _SAFE_MEMBER.match(m.name):
                raise BackupError(f"backup contains an unexpected entry: {m.name!r}")
            names.append(m.name)
        if "manifest.json" not in names or "patternvault.db" not in names:
            raise BackupError("backup is incomplete (manifest or database missing)")
        manifest: dict[str, Any] = json.loads(tar.extractfile("manifest.json").read())  # type: ignore[union-attr]
        if manifest.get("format") != "patternvault-backup" or int(manifest.get("version", 99)) > 1:
            raise BackupError("unsupported backup format")
        return manifest

    def restore(self, name: str, passphrase: str | None = None) -> dict[str, Any]:
        svc = self.svc
        tar = self._open(self._resolve(name).read_bytes(), passphrase)
        stage = Path(tempfile.mkdtemp(prefix="pv-restore-"))
        try:
            with tar:
                manifest = self._validate_members(tar)
                for m in tar.getmembers():
                    target = (stage / m.name).resolve()
                    if stage.resolve() not in target.parents:
                        raise BackupError("backup entry escapes the restore directory")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with tar.extractfile(m) as src, open(target, "wb") as dst:  # type: ignore[union-attr]
                        shutil.copyfileobj(src, dst)
            probe = create_db_engine(stage / "patternvault.db")
            try:
                chk = integrity_check(probe)
                if not chk["ok"]:
                    raise BackupError(f"the database inside the backup failed its integrity check: {chk}")
                rev = current_revision(probe)
                if rev is not None and head_revision() is not None and rev > str(head_revision()):
                    raise BackupError(
                        f"backup schema {rev} is newer than this PatternVault supports ({head_revision()})"
                    )
                run_migrations(probe)  # bring an older schema up to date before swapping it in
            finally:
                probe.dispose()
            safety = self.create(
                None, label=f"automatic safety copy before restoring {name}"
            )  # never lose the current state
            self._carry_over_audit_trail(stage / "patternvault.db")
            self._swap(stage)
            svc.vector_store.clear()
            reindexed = svc.indexer.reindex_all()
            with contextlib.suppress(Exception):
                svc.config._config = svc.config._load()  # pick up the restored config.json
        finally:
            shutil.rmtree(stage, ignore_errors=True)  # pv-ignore: PV-DESTRUCTIVE-OP (our own mkdtemp directory)
        svc.audit.record(
            "user",
            "backup.restore",
            resource_type="backup",
            resource_id=name,
            detail={"counts": manifest.get("counts"), "safety_copy": safety["name"]},
        )
        svc.events.publish("backup.restored", {"name": name})
        return {
            "restored": name,
            "counts": manifest.get("counts"),
            "safety_copy": safety["name"],
            "reindexed": reindexed,
        }

    def _carry_over_audit_trail(self, staged_db: Path) -> None:
        """A restore must never rewind the audit trail: the live instance's log replaces the backup's copy of it.

        (The backup file keeps its own historical trail inside the archive.) The hash chain stays valid because the
        carried-over rows are the live chain, unchanged. Uses ATTACH so every SQL statement is a static string.
        """
        staged = sqlite3.connect(staged_db)
        try:
            staged.execute("ATTACH DATABASE ? AS live", (str(self.svc.paths.db),))
            live_cols = [r[1] for r in staged.execute("PRAGMA live.table_info(audit_logs)")]
            main_cols = [r[1] for r in staged.execute("PRAGMA main.table_info(audit_logs)")]
            if live_cols != main_cols:
                raise BackupError("audit log schema of the backup differs from the live database")
            staged.execute("DELETE FROM main.audit_logs")
            staged.execute("INSERT INTO main.audit_logs SELECT * FROM live.audit_logs ORDER BY id")
            staged.commit()
            staged.execute("DETACH DATABASE live")
        finally:
            staged.close()

    def _swap(self, stage: Path) -> None:
        svc = self.svc
        svc.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(str(svc.paths.db) + suffix).unlink(missing_ok=True)
        shutil.copy2(stage / "patternvault.db", svc.paths.db)
        if (stage / "config.json").exists():
            shutil.copy2(stage / "config.json", svc.paths.config)
        if (stage / "blobs").is_dir():
            for f in (stage / "blobs").rglob("*"):
                if f.is_file():
                    dest = svc.paths.blobs / f.relative_to(stage / "blobs")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dest)
        svc.engine.dispose()
