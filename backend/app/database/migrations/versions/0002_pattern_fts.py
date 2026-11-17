"""FTS5 keyword index for knowledge patterns

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE VIRTUAL TABLE pattern_fts USING fts5("
        "pattern_id UNINDEXED, title, summary, tags, body, tokenize='porter unicode61')"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS pattern_fts")
