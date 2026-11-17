"""Pattern lifecycle and confidence.

observed → generated → approved → tested → verified, plus ``deprecated``. The hard rule (ADR-006):
**a pattern can only become ``tested`` or ``verified`` through a persisted, passing testing session that actually
executed tests.** Nothing else (not user clicks, not model output) can grant those states.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import KnowledgePattern, TestingSession, TestResult
from app.models.base import utcnow

STATUSES = ("observed", "generated", "approved", "tested", "verified", "deprecated")
BASE_CONFIDENCE = {
    "observed": 0.25,
    "generated": 0.20,
    "approved": 0.50,
    "tested": 0.70,
    "verified": 0.88,
    "deprecated": 0.05,
}


class LifecycleError(ValueError):
    pass


def compute_confidence(status: str, success: int, failure: int, rating_sum: int = 0) -> float:
    base = BASE_CONFIDENCE[status]
    n = success + failure
    reuse = ((success + 1) / (n + 2) - 0.5) * 0.2 if n else 0.0
    feedback = max(-0.1, min(0.1, rating_sum * 0.02))
    conf = max(0.01, min(0.99, base + reuse + feedback))
    # only verified patterns may be rated highly
    conf = min(conf, 0.79) if status != "verified" else max(conf, 0.80)
    return round(conf, 3)


def passing_evidence(db: Session, testing_session_id: int) -> tuple[bool, str]:
    """A testing session counts as evidence only if it passed AND executed at least one real test."""
    ts = db.get(TestingSession, testing_session_id)
    if ts is None:
        return False, "testing session not found"
    if ts.status != "passed":
        return False, f"testing session status is '{ts.status}', not 'passed'"
    executed = (
        db.scalar(
            select(func.count())
            .select_from(TestResult)
            .where(TestResult.testing_session_id == ts.id, TestResult.status.in_(("passed", "failed", "error")))
        )
        or 0
    )
    failed = (
        db.scalar(
            select(func.count())
            .select_from(TestResult)
            .where(TestResult.testing_session_id == ts.id, TestResult.status.in_(("failed", "error")))
        )
        or 0
    )
    if executed == 0:
        return False, "no tests were executed in that session"
    if failed:
        return False, f"{failed} test(s) failed in that session"
    return True, "ok"


def is_user_approved(p: KnowledgePattern) -> bool:
    return bool(p.provenance.get("approved_by_user"))


def transition(
    db: Session,
    pattern: KnowledgePattern,
    target: str,
    *,
    testing_session_id: int | None = None,
    rating_sum: int = 0,
) -> str:
    """Apply a lifecycle transition or raise :class:`LifecycleError`. Returns the previous status."""
    if target not in STATUSES:
        raise LifecycleError(f"unknown status '{target}'")
    prev = pattern.status
    if target == prev:
        return prev
    if target == "deprecated":
        pass  # always allowed
    elif prev == "deprecated":
        if target != "observed":
            raise LifecycleError("a deprecated pattern can only be restored to 'observed'")
    elif target == "approved":
        if prev not in ("observed", "generated"):
            raise LifecycleError(f"cannot move {prev} → approved")
    elif target in ("tested", "verified"):
        if testing_session_id is None:
            raise LifecycleError(f"'{target}' requires evidence: a passing testing session")
        ok, why = passing_evidence(db, testing_session_id)
        if not ok:
            raise LifecycleError(f"insufficient evidence for '{target}': {why}")
        if target == "tested" and prev == "verified":
            raise LifecycleError("cannot demote verified → tested; deprecate or edit instead")
        if target == "verified":
            if not (is_user_approved(pattern) or pattern.success_count >= 1):
                raise LifecycleError("'verified' additionally requires user approval or a successful reuse")
            if pattern.verified_by_testing_session_id is None and prev != "tested":
                # allow tested→verified directly; observed/approved→verified must have gone through evidence now
                pass
        pattern.verified_by_testing_session_id = testing_session_id
        if target == "verified":
            pattern.verified_at = utcnow()
    elif target in ("observed", "generated"):
        # demotions happen only as system effects (new version, user restore)
        pattern.verified_by_testing_session_id = None
        pattern.verified_at = None
    pattern.status = target
    pattern.confidence = compute_confidence(target, pattern.success_count, pattern.failure_count, rating_sum)
    return prev
