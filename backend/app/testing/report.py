"""Test report model shared by every testing agent, plus persistence into testing_sessions/test_results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models import TestingSession, TestResult
from app.models.base import utcnow

EXECUTED = ("passed", "failed", "error")


@dataclass
class TestCase:
    __test__ = False  # not a pytest class

    name: str
    status: str  # passed | failed | skipped | error
    description: str = ""
    expected: str = ""
    actual: str = ""
    error_details: str = ""
    correction_applied: str = ""
    retest_status: str | None = None
    duration_ms: int | None = None
    file_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class TestReport:
    __test__ = False

    agent_key: str
    command: str = ""
    cases: list[TestCase] = field(default_factory=list)
    skipped_reason: str = ""
    error: str = ""
    duration_s: float = 0.0
    coverage_percent: float | None = None
    notes: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    testing_session_id: int | None = None

    @property
    def executed(self) -> int:
        return sum(1 for c in self.cases if c.status in EXECUTED)

    @property
    def failures(self) -> list[TestCase]:
        return [c for c in self.cases if c.status in ("failed", "error")]

    @property
    def status(self) -> str:
        """Honesty rules: errors and failures fail; nothing executed is *skipped*, never passed."""
        if self.error:
            return "error"
        if self.failures:
            return "failed"
        if self.skipped_reason or self.executed == 0:
            return "skipped"
        return "passed"

    def summary(self) -> dict[str, Any]:
        counts = {s: sum(1 for c in self.cases if c.status == s) for s in ("passed", "failed", "error", "skipped")}
        return {
            **counts,
            "total": len(self.cases),
            "executed": self.executed,
            "duration_s": round(self.duration_s, 2),
            "skipped_reason": self.skipped_reason,
            "error": self.error,
            "notes": self.notes,
            **self.extra,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_key": self.agent_key,
            "status": self.status,
            "command": self.command,
            "summary": self.summary(),
            "coverage_percent": self.coverage_percent,
            "testing_session_id": self.testing_session_id,
            "cases": [c.to_dict() for c in self.cases],
        }


def persist(
    svc: Any, report: TestReport, *, project_id: int | None, task_id: int | None = None, pattern_id: int | None = None
) -> int:
    with svc.session_factory() as db:
        ts = TestingSession(
            project_id=project_id,
            task_id=task_id,
            pattern_id=pattern_id,
            agent_key=report.agent_key,
            status=report.status,
            command=report.command[:2000],
            summary=report.summary(),
            coverage_percent=report.coverage_percent,
            finished_at=utcnow(),
        )
        db.add(ts)
        db.flush()
        for c in report.cases:
            db.add(
                TestResult(
                    testing_session_id=ts.id,
                    agent_key=report.agent_key,
                    name=c.name[:1000],
                    description=c.description[:4000],
                    expected=c.expected[:4000],
                    actual=c.actual[:4000],
                    status=c.status,
                    error_details=c.error_details[:20000],
                    correction_applied=c.correction_applied,
                    retest_status=c.retest_status,
                    duration_ms=c.duration_ms,
                    file_path=c.file_path,
                )
            )
        db.commit()
        report.testing_session_id = ts.id
        return int(ts.id)


def annotate_correction(
    svc: Any, testing_session_id: int, correction: str, retest_status: str, names: set[str] | None = None
) -> None:
    """Record, on the original failing results, which correction was applied and how the independent retest went."""
    with svc.session_factory() as db:
        for r in db.query(TestResult).filter(
            TestResult.testing_session_id == testing_session_id, TestResult.status.in_(("failed", "error"))
        ):
            if names is None or r.name in names:
                r.correction_applied, r.retest_status = correction[:4000], retest_status
        db.commit()
