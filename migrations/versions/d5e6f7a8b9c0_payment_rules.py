"""payment rules

Wire and ACH wording is detected in code, but some card programs give no text at
all -- a WEX payment arrives with only a card number in the bank detail. A rule
table lets those be identified by which card account they came from.

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd5e6f7a8b9c0'
down_revision: Union[str, Sequence[str], None] = 'c4d5e6f7a8b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "payment_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("phrase", sa.String(), nullable=False),
        sa.Column("item", sa.String(), nullable=False, server_default="bank_detail"),
        sa.Column("rule", sa.String(), nullable=False, server_default="contains"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("note", sa.String(), nullable=False, server_default=""),
    )
    op.create_index("ix_payment_rules_phrase", "payment_rules", ["phrase"])


def downgrade() -> None:
    op.drop_index("ix_payment_rules_phrase", table_name="payment_rules")
    op.drop_table("payment_rules")
