"""regression baselines

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

import app.models.base

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "regression_baselines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("test_key", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("updated_at", app.models.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=op.f("fk_regression_baselines_project_id_projects"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_regression_baselines")),
        sa.UniqueConstraint("project_id", "test_key", name="uq_regression_baselines_project_test"),
    )
    with op.batch_alter_table("regression_baselines", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_regression_baselines_project_id"), ["project_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("regression_baselines", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_regression_baselines_project_id"))
    op.drop_table("regression_baselines")
