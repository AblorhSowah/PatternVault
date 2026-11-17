"""Approvals for restricted operations: persisted requests the user (or UI) decides on; callers await the decision."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.models import Approval
from app.models.base import utcnow


class ApprovalDenied(RuntimeError):
    pass


class ApprovalTimeout(RuntimeError):
    pass


class ApprovalService:
    def __init__(self, session_factory: sessionmaker[Session], audit: Any, events: Any) -> None:
        self._factory = session_factory
        self._audit = audit
        self._events = events
        self._waiters: dict[int, asyncio.Event] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def request(
        self, task_id: int | None, action: str, summary: str, detail: dict[str, Any], requested_by: str = "system"
    ) -> int:
        with self._factory() as db:
            a = Approval(
                task_id=task_id, action=action, summary=summary[:2000], detail=detail, requested_by=requested_by
            )
            db.add(a)
            db.commit()
            aid = a.id
        self._audit.record(
            requested_by,
            "approval.requested",
            resource_type="approval",
            resource_id=aid,
            detail={"action": action, "summary": summary[:300]},
        )
        self._events.publish(
            "approval.requested", {"id": aid, "task_id": task_id, "action": action, "summary": summary[:300]}
        )
        return aid

    def decide(self, approval_id: int, approve: bool, note: str = "", actor: str = "user") -> dict[str, Any]:
        with self._factory() as db:
            a = db.get(Approval, approval_id)
            if a is None:
                raise LookupError("approval not found")
            if a.status != "pending":
                raise ValueError(f"approval already {a.status}")
            a.status, a.decided_at, a.decision_note = ("approved" if approve else "denied"), utcnow(), note[:1000]
            db.commit()
            out = self._to_dict(a)
        self._audit.record(
            actor,
            f"approval.{out['status']}",
            resource_type="approval",
            resource_id=approval_id,
            outcome="allowed" if approve else "denied",
            detail={"action": out["action"], "note": note[:200]},
        )
        self._events.publish(
            "approval.decided", {"id": approval_id, "status": out["status"], "task_id": out["task_id"]}
        )
        ev = self._waiters.get(approval_id)
        if ev is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(ev.set)
        return out

    async def wait(self, approval_id: int, timeout_s: float = 3600.0) -> None:
        """Block until decided. Raises :class:`ApprovalDenied` / :class:`ApprovalTimeout`."""
        self._loop = asyncio.get_running_loop()
        ev = self._waiters.setdefault(approval_id, asyncio.Event())
        deadline = self._loop.time() + timeout_s
        try:
            while True:
                with self._factory() as db:
                    a = db.get(Approval, approval_id)
                    status = a.status if a else "denied"
                if status == "approved":
                    return
                if status == "denied":
                    raise ApprovalDenied("denied by user")
                remaining = deadline - self._loop.time()
                if remaining <= 0:
                    self._expire(approval_id)
                    raise ApprovalTimeout("approval timed out")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(ev.wait(), timeout=min(remaining, 1.0))
                ev.clear()
        finally:
            self._waiters.pop(approval_id, None)

    def _expire(self, approval_id: int) -> None:
        with self._factory() as db:
            a = db.get(Approval, approval_id)
            if a is not None and a.status == "pending":
                a.status, a.decided_at, a.decision_note = "denied", utcnow(), "timed out"
                db.commit()

    def pending(self) -> list[dict[str, Any]]:
        with self._factory() as db:
            return [
                self._to_dict(a) for a in db.query(Approval).filter(Approval.status == "pending").order_by(Approval.id)
            ]

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._factory() as db:
            return [self._to_dict(a) for a in db.query(Approval).order_by(Approval.id.desc()).limit(limit)]

    @staticmethod
    def _to_dict(a: Approval) -> dict[str, Any]:
        return {
            "id": a.id,
            "task_id": a.task_id,
            "action": a.action,
            "summary": a.summary,
            "detail": a.detail,
            "status": a.status,
            "requested_by": a.requested_by,
            "requested_at": a.requested_at.isoformat(),
            "decided_at": a.decided_at.isoformat() if a.decided_at else None,
            "decision_note": a.decision_note,
        }

    def expire_old(self, max_age: timedelta = timedelta(days=1)) -> int:
        n = 0
        with self._factory() as db:
            for a in db.query(Approval).filter(
                Approval.status == "pending", Approval.requested_at < utcnow() - max_age
            ):
                a.status, a.decided_at, a.decision_note = "denied", utcnow(), "expired"
                n += 1
            db.commit()
        return n
