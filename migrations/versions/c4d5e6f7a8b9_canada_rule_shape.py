"""Canada rules as Item / Rule / Input

The table now says which field to read and how to compare it, rather than
implying both from a phrase list. Existing rows become bank-detail contains
rules, which is what they already were.

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c4d5e6f7a8b9'
down_revision: Union[str, Sequence[str], None] = 'b3c4d5e6f7a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("canada_rules", sa.Column("item", sa.String(), nullable=False,
                                            server_default="bank_detail"))
    op.add_column("canada_rules", sa.Column("rule", sa.String(), nullable=False,
                                            server_default="contains"))


def downgrade() -> None:
    op.drop_column("canada_rules", "rule")
    op.drop_column("canada_rules", "item")
