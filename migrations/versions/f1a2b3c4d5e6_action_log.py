"""undoable actions

One row per click, not per charge: a bulk Add over forty charges is one action
and one Undo. The state column holds what each charge looked like beforehand.

Revision ID: f1a2b3c4d5e6
Revises: e0f1a2b3c4d5
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'e0f1a2b3c4d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "actions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("company", sa.String(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("summary", sa.String(), nullable=False, server_default=""),
        sa.Column("state", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("at", sa.DateTime(), nullable=True),
        sa.Column("undone_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_actions_company", "actions", ["company"])
    op.create_index("ix_actions_actor", "actions", ["actor"])
    op.create_index("ix_actions_at", "actions", ["at"])


def downgrade() -> None:
    op.drop_index("ix_actions_at", table_name="actions")
    op.drop_index("ix_actions_actor", table_name="actions")
    op.drop_index("ix_actions_company", table_name="actions")
    op.drop_table("actions")
