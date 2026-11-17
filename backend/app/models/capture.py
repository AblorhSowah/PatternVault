"""Projects, providers, captured sessions, prompts, responses, file snapshots and changes."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UTCDateTime, utcnow

if TYPE_CHECKING:
    from app.models.knowledge import Dependency


class Project(TimestampMixin, Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    root_path: Mapped[str] = mapped_column(String(2048), unique=True, nullable=False)
    languages: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    frameworks: Mapped[list[Any]] = mapped_column(JSON, default=list)  # [{name, version, ecosystem}]
    conventions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    git_remote: Mapped[str | None] = mapped_column(String(2048))
    capture_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    share_knowledge: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    sessions: Mapped[list[DevSession]] = relationship(back_populates="project", cascade="all, delete-orphan")
    dependencies: Mapped[list[Dependency]] = relationship(
        back_populates="project", cascade="all, delete-orphan", foreign_keys="Dependency.project_id"
    )


class AIProvider(TimestampMixin, Base):
    __tablename__ = "ai_providers"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # cloud_assistant | local_llm | manual
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    external_consent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # non-secret settings only


class DevSession(TimestampMixin, Base):
    __tablename__ = "dev_sessions"
    __table_args__ = (
        UniqueConstraint("provider_id", "external_id", name="uq_dev_sessions_provider_external"),
        Index("ix_dev_sessions_project_started", "project_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    provider_id: Mapped[int | None] = mapped_column(ForeignKey("ai_providers.id", ondelete="SET NULL"))
    external_id: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(512), default="")
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)  # active|completed|abandoned
    outcome: Mapped[str] = mapped_column(String(24), default="unknown", nullable=False)  # unknown|passed|failed
    capture_source: Mapped[str] = mapped_column(String(24), default="manual", nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    git_head_start: Mapped[str | None] = mapped_column(String(64))
    git_head_end: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    project: Mapped[Project] = relationship(back_populates="sessions")
    provider: Mapped[AIProvider | None] = relationship()
    prompts: Mapped[list[UserPrompt]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="UserPrompt.sequence"
    )
    responses: Mapped[list[CapturedResponse]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="CapturedResponse.sequence"
    )
    file_changes: Mapped[list[FileChange]] = relationship(back_populates="session", cascade="all, delete-orphan")


class UserPrompt(Base):
    __tablename__ = "user_prompts"
    __table_args__ = (Index("ix_user_prompts_session_seq", "session_id", "sequence"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("dev_sessions.id", ondelete="CASCADE"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)  # already secret-redacted
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    redactions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    session: Mapped[DevSession] = relationship(back_populates="prompts")


class CapturedResponse(Base):
    __tablename__ = "captured_responses"
    __table_args__ = (Index("ix_captured_responses_session_seq", "session_id", "sequence"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("dev_sessions.id", ondelete="CASCADE"), nullable=False)
    prompt_id: Mapped[int | None] = mapped_column(ForeignKey("user_prompts.id", ondelete="SET NULL"))
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), default="assistant", nullable=False)  # assistant|tool
    model: Mapped[str | None] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(Text, nullable=False)  # redacted
    tool_calls: Mapped[list[Any]] = mapped_column(JSON, default=list)  # [{name, input_summary, ok}]
    injection_flags: Mapped[list[Any]] = mapped_column(JSON, default=list)
    redactions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    session: Mapped[DevSession] = relationship(back_populates="responses")


class FileSnapshot(Base):
    """Content-addressed snapshot; the bytes live in ``blobs/<sha256[:2]>/<sha256>`` (already redacted)."""

    __tablename__ = "file_snapshots"
    __table_args__ = (Index("ix_file_snapshots_project_path", "project_id", "path"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("dev_sessions.id", ondelete="SET NULL"))
    path: Mapped[str] = mapped_column(String(2048), nullable=False)  # project-relative, posix
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    language: Mapped[str | None] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(16), default="after", nullable=False)  # before|after|baseline
    redactions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class FileChange(Base):
    __tablename__ = "file_changes"
    __table_args__ = (Index("ix_file_changes_session_path", "session_id", "path"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("dev_sessions.id", ondelete="CASCADE"), nullable=False)
    path: Mapped[str] = mapped_column(String(2048), nullable=False)
    change_type: Mapped[str] = mapped_column(String(16), nullable=False)  # created|modified|deleted|renamed
    old_path: Mapped[str | None] = mapped_column(String(2048))
    before_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("file_snapshots.id", ondelete="SET NULL"))
    after_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("file_snapshots.id", ondelete="SET NULL"))
    diff: Mapped[str] = mapped_column(Text, default="")  # unified diff, redacted, size-capped
    lines_added: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lines_removed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source: Mapped[str] = mapped_column(String(24), default="workspace", nullable=False)  # workspace|adapter|git
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    session: Mapped[DevSession] = relationship(back_populates="file_changes")
    before_snapshot: Mapped[FileSnapshot | None] = relationship(foreign_keys=[before_snapshot_id])
    after_snapshot: Mapped[FileSnapshot | None] = relationship(foreign_keys=[after_snapshot_id])
