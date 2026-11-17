"""ORM models. Importing this package registers every table on ``Base.metadata``."""

from app.models.base import Base
from app.models.capture import (
    AIProvider,
    CapturedResponse,
    DevSession,
    FileChange,
    FileSnapshot,
    Project,
    UserPrompt,
)
from app.models.chat import ChatConversation, ChatMessage
from app.models.execution import (
    AgentDefinition,
    AgentExecution,
    Approval,
    AuditLog,
    DebuggingSolution,
    ErrorRecord,
    RecoveryPoint,
    RegressionBaseline,
    Task,
    TestingSession,
    TestResult,
)
from app.models.knowledge import (
    CodeImplementation,
    Dependency,
    EmbeddingIndexEntry,
    KnowledgePattern,
    PatternRelationship,
    PatternVersion,
    UserFeedback,
)

__all__ = [
    "AIProvider",
    "AgentDefinition",
    "AgentExecution",
    "Approval",
    "AuditLog",
    "Base",
    "CapturedResponse",
    "ChatConversation",
    "ChatMessage",
    "CodeImplementation",
    "DebuggingSolution",
    "Dependency",
    "DevSession",
    "EmbeddingIndexEntry",
    "ErrorRecord",
    "FileChange",
    "FileSnapshot",
    "KnowledgePattern",
    "PatternRelationship",
    "PatternVersion",
    "Project",
    "RecoveryPoint",
    "RegressionBaseline",
    "Task",
    "TestResult",
    "TestingSession",
    "UserFeedback",
    "UserPrompt",
]
