"""file format on source accounts

The CSV parser was selected by the source key, so a card account could only be
called "slash", "divvy" or "wex". Naming the format separately lets the key be
whatever the person wants. Empty means "same as source", which is what the
original three are.

Revision ID: a6b7c8d9e0f1
Revises: f5a6b7c8d9e0
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a6b7c8d9e0f1'
down_revision: Union[str, Sequence[str], None] = 'f5a6b7c8d9e0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("source_accounts",
                  sa.Column("file_format", sa.String(), nullable=False,
                            server_default=""))


def downgrade() -> None:
    op.drop_column("source_accounts", "file_format")
