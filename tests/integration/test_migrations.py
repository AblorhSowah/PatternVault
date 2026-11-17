"""Test plan: database/engine.py + migrations
- fresh DB migrates to head; ORM metadata has zero drift against the migrated schema
- downgrade to base then upgrade again works (reversible)
- FK enforcement + WAL are on; integrity_check is clean
- cascade rules: deleting a project removes its sessions/prompts/changes but not global knowledge
- all 20 required entity groups exist
"""

from pathlib import Path

import pytest
from sqlalchemy import inspect, select, text

from app.database import (
    create_db_engine,
    current_revision,
    head_revision,
    integrity_check,
    make_session_factory,
    run_migrations,
    schema_drift,
)
from app.database.engine import downgrade_migrations
from app.models import (
    CapturedResponse,
    DevSession,
    FileChange,
    KnowledgePattern,
    PatternVersion,
    Project,
    UserPrompt,
)

pytestmark = pytest.mark.integration

REQUIRED_TABLES = {
    "projects",
    "ai_providers",
    "dev_sessions",
    "user_prompts",
    "captured_responses",
    "file_snapshots",
    "file_changes",
    "knowledge_patterns",
    "chat_conversations",
    "chat_messages",
    "pattern_versions",
    "pattern_relationships",
    "dependencies",
    "code_implementations",
    "agent_definitions",
    "agent_executions",
    "testing_sessions",
    "test_results",
    "errors",
    "debugging_solutions",
    "user_feedback",
    "audit_logs",
    "tasks",
    "approvals",
    "recovery_points",
}


@pytest.fixture()
def engine(tmp_path: Path):
    e = create_db_engine(tmp_path / "t.db")
    run_migrations(e)
    yield e
    e.dispose()


def test_migrates_to_head(engine):
    assert current_revision(engine) == head_revision()
    assert REQUIRED_TABLES <= set(inspect(engine).get_table_names())


def test_no_schema_drift(engine):
    assert schema_drift(engine) == []


def test_migration_is_reversible(engine):
    downgrade_migrations(engine)
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    run_migrations(engine)
    assert REQUIRED_TABLES <= set(inspect(engine).get_table_names())


def test_pragmas(engine):
    with engine.connect() as c:
        assert c.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert c.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"
    assert integrity_check(engine)["ok"]


def test_foreign_keys_enforced(engine):
    sf = make_session_factory(engine)
    with sf() as s:
        s.add(DevSession(project_id=9999, title="orphan"))
        with pytest.raises(Exception, match="FOREIGN KEY"):
            s.commit()


def test_project_delete_cascades_capture_but_keeps_knowledge(engine):
    sf = make_session_factory(engine)
    with sf() as s:
        proj = Project(name="p", root_path="/tmp/p")
        s.add(proj)
        s.flush()
        sess = DevSession(project_id=proj.id, title="s")
        s.add(sess)
        s.flush()
        s.add_all(
            [
                UserPrompt(session_id=sess.id, sequence=1, content="hi", content_hash="h"),
                CapturedResponse(session_id=sess.id, sequence=1, content="yo"),
                FileChange(session_id=sess.id, path="a.py", change_type="created"),
            ]
        )
        pat = KnowledgePattern(title="auth", fingerprint="f", project_id=proj.id, origin_session_id=sess.id)
        s.add(pat)
        s.commit()
        s.delete(proj)
        s.commit()
        s.expire_all()  # DB-level ON DELETE SET NULL happens behind the ORM's back
        assert s.scalars(select(DevSession)).all() == []
        assert s.scalars(select(UserPrompt)).all() == []
        kept = s.scalars(select(KnowledgePattern)).one()
        assert kept.project_id is None and kept.origin_session_id is None


def test_pattern_versions_unique_per_number(engine):
    sf = make_session_factory(engine)
    with sf() as s:
        p = KnowledgePattern(title="t", fingerprint="f")
        s.add(p)
        s.flush()
        s.add(PatternVersion(pattern_id=p.id, version_number=1, title="t", content_hash="h"))
        s.commit()
        s.add(PatternVersion(pattern_id=p.id, version_number=1, title="t", content_hash="h2"))
        with pytest.raises(Exception, match="UNIQUE"):
            s.commit()


def test_datetimes_roundtrip_as_utc(engine):
    from datetime import UTC

    sf = make_session_factory(engine)
    with sf() as s:
        s.add(Project(name="p", root_path="/x"))
        s.commit()
        got = s.scalars(select(Project)).one()
        assert got.created_at.tzinfo is not None and got.created_at.utcoffset().total_seconds() == 0
        assert got.created_at.tzinfo == UTC or got.created_at.utcoffset() == UTC.utcoffset(None)
