"""Dependency container: owns config, database, audit log and the event bus for one backend process."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.agents.registry import AgentRegistry
from app.config import ConfigManager, Paths, RuntimeSettings
from app.database import create_db_engine, make_session_factory, run_migrations
from app.execution.commands import CommandExecutor
from app.execution.file_ops import FileOps
from app.execution.recovery import RecoveryManager
from app.generation.generator import CodeGenerator
from app.knowledge.backup import BackupService
from app.knowledge.portability import Portability
from app.knowledge.quality import QualityAnalyzer
from app.knowledge.service import PatternService
from app.knowledge.storage import StorageMonitor
from app.learning.adapters.registry import AdapterRegistry
from app.learning.blobs import BlobStore
from app.learning.capture import CaptureService
from app.learning.pipeline import LearningService
from app.orchestration.approvals import ApprovalService
from app.orchestration.events import EventBus
from app.orchestration.orchestrator import Orchestrator
from app.replay.replayer import Replayer
from app.retrieval.context import ContextAnalyzer
from app.retrieval.indexer import EmbedderProvider, PatternIndexer
from app.retrieval.search import KnowledgeSearch
from app.retrieval.vector_store import ChromaVectorStore, SqliteVectorStore, VectorStore
from app.security.audit import AuditLogger
from app.security.exclusions import ExclusionPolicy
from app.services.modes import ModeController

log = logging.getLogger("patternvault")


class Services:
    def __init__(
        self,
        settings: RuntimeSettings | None = None,
        home: Path | None = None,
        adapters: AdapterRegistry | None = None,
    ) -> None:
        self.settings = settings or RuntimeSettings()
        if home is not None:
            self.settings.home = home
        self.paths = Paths(Path(self.settings.home).expanduser())
        self.paths.ensure()
        self.config = ConfigManager(self.paths.config)
        self.engine: Engine = create_db_engine(self.paths.db)
        run_migrations(self.engine)
        self.session_factory: sessionmaker[Session] = make_session_factory(self.engine)
        self.audit = AuditLogger(self.session_factory)
        self.events = EventBus()
        self.started_monotonic = time.monotonic()
        self.blobs = BlobStore(self.paths.blobs)
        self.storage = StorageMonitor(self)  # lazily reads services; constructed early because capture guards use it
        self.adapters = adapters or AdapterRegistry()
        self.patterns = PatternService(self.session_factory, self.audit, self.events)
        self.capture = CaptureService(
            self.blobs,
            self.adapters,
            self.audit,
            self.events,
            self.exclusion_policy,
            lambda: self.storage.ensure_capture_allowed(),
        )
        self.learning = LearningService(
            self.session_factory, self.blobs, self.patterns, self.audit, self.events, self.exclusion_policy
        )

        backend = self.config.config.storage.vector_backend
        self.vector_store: VectorStore = (
            ChromaVectorStore(self.paths.home / "chroma")
            if backend == "chroma"
            else SqliteVectorStore(self.paths.vectors)
        )
        self.embedders = EmbedderProvider(lambda: self.config.config)
        self.indexer = PatternIndexer(self.session_factory, self.vector_store, self.embedders)
        self.patterns.listeners.append(self.indexer.on_pattern_event)
        self.search = KnowledgeSearch(
            self.session_factory, self.vector_store, self.embedders, self.patterns, lambda: self.config.config
        )
        self.context = ContextAnalyzer(self.session_factory, self.search)
        self.quality = QualityAnalyzer(
            self.session_factory, self.vector_store, self.embedders, self.patterns, self.audit
        )
        self.modes = ModeController(lambda: self.config.config, self.audit, self.events)
        self.generator = CodeGenerator(self)
        self.replayer = Replayer(self)
        self.approvals = ApprovalService(self.session_factory, self.audit, self.events)
        self.recovery = RecoveryManager(self.paths.recovery, self.session_factory, self.audit)
        self.file_ops = FileOps(self.recovery, self.approvals, self.audit, self.events)
        self.commands = CommandExecutor(self.approvals, self.audit, self.events)
        self.agents = AgentRegistry(self)
        self.orchestrator = Orchestrator(self)
        self.backups = BackupService(self)
        self.portability = Portability(self.session_factory, self.patterns, self.indexer, self.audit)

    def exclusion_policy(self) -> ExclusionPolicy:
        cfg = self.config.config.privacy
        return ExclusionPolicy(extra_globs=cfg.exclude_globs, max_file_bytes=cfg.max_file_bytes)

    def close(self) -> None:
        self.engine.dispose()
