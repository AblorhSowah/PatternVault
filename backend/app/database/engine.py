"""SQLite engine/session factories and the programmatic Alembic runner."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def include_object(obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any) -> bool:
    """Ignore SQLite FTS5 virtual/shadow tables (managed by raw-SQL migrations, not ORM models)."""
    return not (type_ == "table" and name is not None and name.startswith("pattern_fts"))


def create_db_engine(db_path: Path | str) -> Engine:
    """Create a SQLite engine with WAL, foreign keys enforced and a sane busy timeout."""
    url = f"sqlite:///{db_path}"
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 15}, future=True)

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn: Any, _record: Any) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error."""
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def _alembic_config(engine: Engine) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False).replace("%", "%%"))
    return cfg


def run_migrations(engine: Engine, target: str = "head") -> None:
    cfg = _alembic_config(engine)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, target)


def downgrade_migrations(engine: Engine, target: str = "base") -> None:
    cfg = _alembic_config(engine)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, target)


def head_revision() -> str | None:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return ScriptDirectory.from_config(cfg).get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def schema_drift(engine: Engine) -> list[Any]:
    """Differences between the migrated database and the ORM metadata (empty list = in sync)."""
    with engine.connect() as conn:
        ctx = MigrationContext.configure(
            conn, opts={"compare_type": True, "render_as_batch": True, "include_object": include_object}
        )
        return list(compare_metadata(ctx, Base.metadata))


def integrity_check(engine: Engine) -> dict[str, Any]:
    """Run SQLite integrity + foreign-key checks."""
    with engine.connect() as conn:
        integrity = [r[0] for r in conn.execute(text("PRAGMA integrity_check")).fetchall()]
        fk = [tuple(r) for r in conn.execute(text("PRAGMA foreign_key_check")).fetchall()]
    return {"integrity": integrity, "foreign_key_violations": fk, "ok": integrity == ["ok"] and not fk}
