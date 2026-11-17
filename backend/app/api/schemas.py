"""Pydantic request/response schemas for the REST API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.config import Mode


class Health(BaseModel):
    status: str
    version: str
    api_version: str


class ModeRequest(BaseModel):
    mode: Mode


class ProjectCreate(BaseModel):
    root_path: str = Field(min_length=1, max_length=2048)
    name: str | None = Field(default=None, max_length=255)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    capture_enabled: bool | None = None
    share_knowledge: bool | None = None


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    root_path: str
    languages: dict[str, Any]
    frameworks: list[Any]
    conventions: dict[str, Any]
    capture_enabled: bool
    share_knowledge: bool
    created_at: datetime
    updated_at: datetime


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ts: datetime
    actor: str
    action: str
    resource_type: str
    resource_id: str
    outcome: str
    permission_level: str
    detail: dict[str, Any]


class SessionStart(BaseModel):
    project_id: int
    title: str = Field(default="", max_length=512)
    provider: str = "manual"


class SessionEnd(BaseModel):
    outcome: str | None = Field(default=None, pattern="^(passed|failed|unknown)$")
    extract: bool = True


class TextEntry(BaseModel):
    text: str = Field(min_length=1, max_length=400_000)
    model: str | None = Field(default=None, max_length=128)


class ImportRequest(BaseModel):
    project_id: int
    external_id: str = Field(min_length=1, max_length=255)
    force: bool = False
    extract: bool = True


class PatternUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=512)
    summary: str | None = None
    tags: list[str] | None = None
    category: str | None = Field(default=None, max_length=64)


class FeedbackIn(BaseModel):
    rating: int | None = Field(default=None, ge=-5, le=5)
    comment: str = Field(default="", max_length=5000)
    action: str = Field(default="rate", pattern="^(rate|comment|correct|approve|reject)$")
    correction: dict[str, Any] | None = None


class RelateIn(BaseModel):
    target_id: int
    relation: str
    score: float = 0.0
    note: str = ""


class SearchRequest(BaseModel):
    query: str = Field(max_length=2000)
    project_id: int | None = None
    language: str | None = None
    framework: str | None = None
    framework_version: str | None = None
    categories: list[str] | None = None
    statuses: list[str] | None = None
    include_deprecated: bool = False
    cross_project: bool = True
    top_k: int = Field(default=10, ge=1, le=50)


class ContextRequest(BaseModel):
    request: str = Field(min_length=1, max_length=8000)
    project_id: int | None = None
    top_k: int = Field(default=8, ge=1, le=30)


class ModelSelect(BaseModel):
    model: str | None = Field(default=None, max_length=200)
    embedding_model: str | None = Field(default=None, max_length=200)
    force: bool = False


class ModelPull(BaseModel):
    model: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:/-]+$")


class GenerateIn(BaseModel):
    request: str = Field(min_length=3, max_length=8000)
    project_id: int | None = None
    mode: str = Field(default="auto", pattern="^(auto|new|adapt)$")
    pattern_ids: list[int] | None = None
    model: str | None = Field(default=None, max_length=200)
    backend: str = Field(default="auto", pattern="^(auto|local|cloud|compare)$")
    max_repairs: int = Field(default=2, ge=0, le=5)
    top_k: int = Field(default=3, ge=0, le=8)


class ReplayIn(BaseModel):
    pattern_id: int
    project_id: int
    path_map: dict[str, str] | None = None
    include_tests: bool = True
    selected_paths: list[str] | None = None


class CompareIn(BaseModel):
    a: int
    b: int


class TaskIn(BaseModel):
    kind: str = Field(pattern="^(generate|replay|debug|review|test|document)$")
    request: str = Field(min_length=1, max_length=8000)
    project_id: int | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class AgentUpdate(BaseModel):
    enabled: bool


class ApprovalDecision(BaseModel):
    approve: bool
    note: str = Field(default="", max_length=1000)


class TestRunIn(BaseModel):
    project_id: int
    agents: list[str] | None = None
    fix: bool = False
    params: dict[str, Any] = Field(default_factory=dict)


class BackupCreate(BaseModel):
    passphrase: str | None = Field(default=None, max_length=256)
    label: str = Field(default="", max_length=120)


class BackupRestore(BaseModel):
    name: str = Field(max_length=200)
    passphrase: str | None = Field(default=None, max_length=256)
    confirm: bool = False


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    project_id: int | None = None


class ConversationUpdate(BaseModel):
    project_id: int | None = None


class ChatMessageIn(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    backend: str = Field(default="auto", pattern="^(auto|local|cloud)$")
    model: str | None = Field(default=None, max_length=200)
    use_knowledge: bool = True
    top_k: int = Field(default=4, ge=0, le=8)
