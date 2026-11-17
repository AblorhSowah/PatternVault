"""Content-addressed blob store for (already redacted) file snapshots and recovery data."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, digest: str) -> Path:
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid blob digest")
        return self.root / digest[:2] / digest

    def put(self, data: bytes) -> str:
        digest = sha256_hex(data)
        path = self._path(digest)
        if path.exists():
            return digest
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".blob-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return digest

    def get(self, digest: str) -> bytes | None:
        path = self._path(digest)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        if sha256_hex(data) != digest:
            raise OSError(f"blob {digest[:12]} is corrupt (hash mismatch)")
        return data

    def get_text(self, digest: str) -> str | None:
        data = self.get(digest)
        return None if data is None else data.decode("utf-8", errors="replace")

    def exists(self, digest: str) -> bool:
        return self._path(digest).exists()

    def delete(self, digest: str) -> None:
        self._path(digest).unlink(missing_ok=True)

    def total_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
