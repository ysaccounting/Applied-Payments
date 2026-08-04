"""tunable match weights

The scorecard was constants in engine/suggest.py, so tuning it meant a deploy.
An empty table behaves exactly as the code does -- rows only override.

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f7a8b9c0d1e2'
down_revision: Union[str, Sequence[str], None] = 'e6f7a8b9c0d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "match_weights",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("weight", sa.Float(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("match_weights")
