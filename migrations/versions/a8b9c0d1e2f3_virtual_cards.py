"""flag virtual-card programs

Slash and Divvy issue a number per purchase; WEX and a bank card do not. The
distinction changes what a CC Last 4 identifies, so it is worth showing.

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a8b9c0d1e2f3'
down_revision: Union[str, Sequence[str], None] = 'f7a8b9c0d1e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("source_accounts", sa.Column("is_virtual", sa.Boolean(),
                                               nullable=False,
                                               server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("source_accounts", "is_virtual")
