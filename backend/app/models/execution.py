"""Agents, tasks, executions, approvals, recovery points, testing, errors, debugging solutions, audit log."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UTCDateTime, utcnow


class AgentDefinition(TimestampMixin, Base):
    __tablename__ = "agent_definitions"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(24), nullable=False)  # development | testing | orchestration
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    permissions: Mapped[list[Any]] = mapped_column(JSON, default=list)  # capability names the agent may use
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Task(Base):
    """An orchestrated development request and its execution history."""

    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_project_created", "project_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(24), nullable=False)  # generate|replay|debug|review|test|document
    request: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    # pending|running|awaiting_approval|completed|verified|failed|cancelled
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    permission_level: Mapped[str] = mapped_column(String(32), nullable=False)
    plan: Mapped[list[Any]] = mapped_column(JSON, default=list)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    executions: Mapped[list[AgentExecution]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="AgentExecution.id"
    )


class AgentExecution(Base):
    __tablename__ = "agent_executions"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agent_definitions.id", ondelete="SET NULL"), index=True)
    agent_key: Mapped[str] = mapped_column(String(64), nullable=False)
    step_key: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="running", nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    task: Mapped[Task | None] = relationship(back_populates="executions")


class Approval(Base):
    """A pending/decided request for a restricted operation."""

    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(48), nullable=False)  # write_file|run_command|install_dependency|...
    summary: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)  # pending|approved|denied
    requested_by: Mapped[str] = mapped_column(String(64), default="system", nullable=False)
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    decision_note: Mapped[str] = mapped_column(Text, default="")


class RecoveryPoint(Base):
    __tablename__ = "recovery_points"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    label: Mapped[str] = mapped_column(String(255), default="")
    # [{path, hash|null (null = did not exist)}]; bytes are stored in the blob store.
    files: Mapped[list[Any]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)  # active|restored|expired
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    restored_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class TestingSession(Base):
    __tablename__ = "testing_sessions"
    __table_args__ = (Index("ix_testing_sessions_project_started", "project_id", "started_at"),)
    __test__ = False  # not a pytest class

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), index=True)
    pattern_id: Mapped[int | None] = mapped_column(ForeignKey("knowledge_patterns.id", ondelete="SET NULL"))
    agent_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    # running|passed|failed|error|skipped
    command: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    coverage_percent: Mapped[float | None] = mapped_column(Float)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    results: Mapped[list[TestResult]] = relationship(back_populates="testing_session", cascade="all, delete-orphan")


class TestResult(Base):
    __tablename__ = "test_results"
    __test__ = False

    id: Mapped[int] = mapped_column(primary_key=True)
    testing_session_id: Mapped[int] = mapped_column(
        ForeignKey("testing_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(1024), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    expected: Mapped[str] = mapped_column(Text, default="")
    actual: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # passed|failed|skipped|error
    error_details: Mapped[str] = mapped_column(Text, default="")
    correction_applied: Mapped[str] = mapped_column(Text, default="")
    retest_status: Mapped[str | None] = mapped_column(String(16))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    file_path: Mapped[str | None] = mapped_column(String(2048))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    testing_session: Mapped[TestingSession] = relationship(back_populates="results")


class ErrorRecord(Base):
    __tablename__ = "errors"
    __table_args__ = (Index("ix_errors_signature", "signature"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), index=True)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("dev_sessions.id", ondelete="SET NULL"))
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # compiler|runtime|test|agent|security
    message: Mapped[str] = mapped_column(Text, nullable=False)
    stack: Mapped[str] = mapped_column(Text, default="")
    signature: Mapped[str] = mapped_column(String(64), nullable=False)  # normalised hash for matching
    file_path: Mapped[str | None] = mapped_column(String(2048))
    line: Mapped[int | None] = mapped_column(Integer)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class DebuggingSolution(Base):
    __tablename__ = "debugging_solutions"

    id: Mapped[int] = mapped_column(primary_key=True)
    error_id: Mapped[int | None] = mapped_column(ForeignKey("errors.id", ondelete="SET NULL"))
    signature: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    root_cause: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    fix: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {files:[{path, diff}]}
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    verified_by_testing_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("testing_sessions.id", ondelete="SET NULL")
    )
    use_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class AuditLog(Base):
    """Append-only, hash-chained audit trail (see security/audit.py)."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(96), nullable=False)  # user | system | agent:<key>
    action: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(48), default="", nullable=False)
    resource_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), default="allowed", nullable=False)  # allowed|denied|error
    permission_level: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)


class RegressionBaseline(Base):
    """Last known-good result per test, used by the regression testing agent."""

    __tablename__ = "regression_baselines"
    __table_args__ = (UniqueConstraint("project_id", "test_key", name="uq_regression_baselines_project_test"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    test_key: Mapped[str] = mapped_column(String(1024), nullable=False)  # "<agent>::<test name>"
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False)
