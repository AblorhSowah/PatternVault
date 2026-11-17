"""Append-only, hash-chained audit log."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import AuditLog
from app.models.base import utcnow
from app.security.secrets import redact_obj


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _entry_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + _canonical(payload)).encode()).hexdigest()


def _payload(
    ts: datetime, actor: str, action: str, rtype: str, rid: str, outcome: str, level: str, detail: dict[str, Any]
) -> dict[str, Any]:
    return {
        "ts": ts.isoformat(),
        "actor": actor,
        "action": action,
        "resource_type": rtype,
        "resource_id": rid,
        "outcome": outcome,
        "permission_level": level,
        "detail": detail,
    }


class AuditLogger:
    """Writes audit entries. Details are secret-redacted before hashing and storage.

    The logger uses its *own* short transaction on a separate connection so entries survive a rolled-back
    caller transaction (e.g. a denied operation). Consequently callers must **not** hold an open write
    transaction on the same SQLite database while calling :meth:`record` (commit first), or the two
    connections will contend for SQLite's single write lock.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._factory = session_factory
        self._lock = threading.Lock()

    def record(
        self,
        actor: str,
        action: str,
        *,
        resource_type: str = "",
        resource_id: str | int = "",
        outcome: str = "allowed",
        permission_level: str = "",
        detail: dict[str, Any] | None = None,
    ) -> int:
        clean_detail = json.loads(json.dumps(redact_obj(detail or {}), default=str))
        ts = utcnow()
        with self._lock, self._factory() as session:
            last = session.execute(select(AuditLog.hash).order_by(AuditLog.id.desc()).limit(1)).scalar_one_or_none()
            prev = last or ""
            payload = _payload(
                ts, actor, action, resource_type, str(resource_id), outcome, permission_level, clean_detail
            )
            row = AuditLog(
                ts=ts,
                actor=actor,
                action=action,
                resource_type=resource_type,
                resource_id=str(resource_id),
                outcome=outcome,
                permission_level=permission_level,
                detail=clean_detail,
                prev_hash=prev,
                hash=_entry_hash(prev, payload),
            )
            session.add(row)
            session.commit()
            return row.id


def verify_chain(session: Session) -> dict[str, Any]:
    """Recompute the chain. Returns ``{ok, checked, first_bad_id}``."""
    prev = ""
    checked = 0
    for row in session.execute(select(AuditLog).order_by(AuditLog.id)).scalars():
        payload = _payload(
            row.ts,
            row.actor,
            row.action,
            row.resource_type,
            row.resource_id,
            row.outcome,
            row.permission_level,
            row.detail,
        )
        if row.prev_hash != prev or row.hash != _entry_hash(prev, payload):
            return {"ok": False, "checked": checked, "first_bad_id": row.id}
        prev = row.hash
        checked += 1
    return {"ok": True, "checked": checked, "first_bad_id": None}
