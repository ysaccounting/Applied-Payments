"""user prefs

Per-user UI preferences (column order, visibility, widths). Stored server-side
so a layout follows the person rather than the browser.

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e4f5a6b7c8d9'
down_revision: Union[str, Sequence[str], None] = 'd3e4f5a6b7c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_prefs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_prefs_username", "user_prefs", ["username"])
    op.create_index("ix_user_prefs_user_key", "user_prefs",
                    ["username", "key"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_user_prefs_user_key", table_name="user_prefs")
    op.drop_index("ix_user_prefs_username", table_name="user_prefs")
    op.drop_table("user_prefs")
