"""Alembic environment. The connection is supplied by ``app.database.engine.run_migrations``."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import Connection

from app.database.engine import include_object
from app.models import Base

target_metadata = Base.metadata


def run_migrations_online() -> None:
    connection = context.config.attributes.get("connection")
    if connection is None:  # CLI use: alembic -x ... / sqlalchemy.url from config
        engine = create_engine(context.config.get_main_option("sqlalchemy.url") or "", poolclass=pool.NullPool)
        with engine.connect() as conn:
            _run(conn)
    else:
        _run(connection)


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,  # SQLite needs batch mode for ALTER
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("Offline migrations are not supported")
run_migrations_online()
