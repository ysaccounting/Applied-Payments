"""stored match strength on charges

Tier and score were computed per request, so the database could not filter or
sort by them -- the browser could only narrow the page it had already fetched.
Storing them makes Strength behave like every other filter.

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f5a6b7c8d9e0'
down_revision: Union[str, Sequence[str], None] = 'e4f5a6b7c8d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("charges", sa.Column("tier", sa.String(), nullable=False,
                                       server_default="none"))
    op.add_column("charges", sa.Column("score", sa.Integer(), nullable=False,
                                       server_default="0"))
    op.create_index("ix_charges_tier", "charges", ["tier"])


def downgrade() -> None:
    op.drop_index("ix_charges_tier", table_name="charges")
    op.drop_column("charges", "score")
    op.drop_column("charges", "tier")
