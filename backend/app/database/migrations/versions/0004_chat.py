"""chat conversations and messages

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

import app.models.base

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_conversations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("created_at", app.models.base.UTCDateTime(), nullable=False),
        sa.Column("updated_at", app.models.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=op.f("fk_chat_conversations_project_id_projects"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_conversations")),
    )
    with op.batch_alter_table("chat_conversations", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_chat_conversations_project_id"), ["project_id"], unique=False)

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=False),
        sa.Column("created_at", app.models.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["chat_conversations.id"], name=op.f("fk_chat_messages_conversation_id_chat_conversations"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_messages")),
    )
    with op.batch_alter_table("chat_messages", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_chat_messages_conversation_id"), ["conversation_id"], unique=False)
        batch_op.create_index("ix_chat_messages_conversation_id_id", ["conversation_id", "id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("chat_messages", schema=None) as batch_op:
        batch_op.drop_index("ix_chat_messages_conversation_id_id")
        batch_op.drop_index(batch_op.f("ix_chat_messages_conversation_id"))
    op.drop_table("chat_messages")
    with op.batch_alter_table("chat_conversations", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_chat_conversations_project_id"))
    op.drop_table("chat_conversations")
