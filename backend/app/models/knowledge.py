"""Knowledge patterns, versions, relationships, dependencies, implementations and feedback."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UTCDateTime, new_uid, utcnow

if TYPE_CHECKING:
    from app.models.capture import Project

# Lifecycle states, ordered by trust. ``deprecated`` is a side state.
PATTERN_STATUSES = ("observed", "generated", "approved", "tested", "verified", "deprecated")
PATTERN_RELATIONS = ("duplicate_of", "conflicts_with", "supersedes", "depends_on", "related_to", "variant_of")


class KnowledgePattern(TimestampMixin, Base):
    __tablename__ = "knowledge_patterns"
    __table_args__ = (
        Index("ix_knowledge_patterns_status", "status"),
        Index("ix_knowledge_patterns_category", "category"),
        Index("ix_knowledge_patterns_lang_fw", "language", "framework"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    uid: Mapped[str] = mapped_column(String(32), unique=True, default=new_uid, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(64), default="general", nullable=False)
    tags: Mapped[list[Any]] = mapped_column(JSON, default=list)
    language: Mapped[str | None] = mapped_column(String(32))
    framework: Mapped[str | None] = mapped_column(String(64))
    framework_version: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="observed", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.1, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    origin_session_id: Mapped[int | None] = mapped_column(ForeignKey("dev_sessions.id", ondelete="SET NULL"))
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    use_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Weak reference (no FK): verification evidence must survive testing-session cleanup, and a FK here
    # would create a patterns <-> testing_sessions dependency cycle.
    verified_by_testing_session_id: Mapped[int | None] = mapped_column(Integer, index=True)
    current_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("pattern_versions.id", ondelete="SET NULL", use_alter=True, name="fk_patterns_current_version")
    )

    versions: Mapped[list[PatternVersion]] = relationship(
        back_populates="pattern",
        cascade="all, delete-orphan",
        foreign_keys="PatternVersion.pattern_id",
        order_by="PatternVersion.version_number",
    )
    current_version: Mapped[PatternVersion | None] = relationship(foreign_keys=[current_version_id], post_update=True)


class PatternVersion(Base):
    __tablename__ = "pattern_versions"
    __table_args__ = (UniqueConstraint("pattern_id", "version_number", name="uq_pattern_versions_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    pattern_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_patterns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    # Structured content: {kind, request, files:[{path, role, language, content, symbols}], steps, tests, ...}
    content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    change_note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(32), default="extraction", nullable=False)
    parent_version_id: Mapped[int | None] = mapped_column(ForeignKey("pattern_versions.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    pattern: Mapped[KnowledgePattern] = relationship(back_populates="versions", foreign_keys=[pattern_id])


class PatternRelationship(Base):
    __tablename__ = "pattern_relationships"
    __table_args__ = (UniqueConstraint("source_id", "target_id", "relation", name="uq_pattern_relationships_triple"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_patterns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_patterns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relation: Mapped[str] = mapped_column(String(24), nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class Dependency(Base):
    """A dependency of a project, or one required by a pattern (``pattern_id`` set)."""

    __tablename__ = "dependencies"
    __table_args__ = (Index("ix_dependencies_project_name", "project_id", "ecosystem", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    pattern_id: Mapped[int | None] = mapped_column(ForeignKey("knowledge_patterns.id", ondelete="CASCADE"))
    ecosystem: Mapped[str] = mapped_column(String(16), nullable=False)  # pypi|npm|composer|cargo|go|maven
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version_spec: Mapped[str] = mapped_column(String(128), default="")
    is_dev: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source_file: Mapped[str | None] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    project: Mapped[Project | None] = relationship(back_populates="dependencies", foreign_keys=[project_id])


class CodeImplementation(Base):
    """A concrete implementation: extracted from a session, generated, or replayed."""

    __tablename__ = "code_implementations"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), index=True)
    pattern_id: Mapped[int | None] = mapped_column(ForeignKey("knowledge_patterns.id", ondelete="SET NULL"), index=True)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("dev_sessions.id", ondelete="SET NULL"))
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # extracted|generated|replayed|adapted
    request: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(128))
    files: Mapped[list[Any]] = mapped_column(JSON, default=list)  # [{path, content_hash, snapshot_id?}]
    status: Mapped[str] = mapped_column(String(16), default="proposed", nullable=False)  # proposed|applied|rejected
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class UserFeedback(Base):
    __tablename__ = "user_feedback"
    __table_args__ = (Index("ix_user_feedback_target", "target_type", "target_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    target_type: Mapped[str] = mapped_column(String(24), nullable=False)  # pattern|implementation|session|task
    target_id: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # approve|reject|correct|comment|deprecate|rate
    rating: Mapped[int | None] = mapped_column(Integer)
    comment: Mapped[str] = mapped_column(Text, default="")
    correction: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class EmbeddingIndexEntry(Base):
    """Tracks which pattern versions have vectors, and with which model (vectors live in vectors.db)."""

    __tablename__ = "embedding_index"

    id: Mapped[int] = mapped_column(primary_key=True)
    pattern_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_patterns.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    version_id: Mapped[int] = mapped_column(Integer, nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    indexed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
