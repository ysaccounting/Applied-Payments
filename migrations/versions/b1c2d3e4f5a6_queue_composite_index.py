"""queue composite index

Covers the access pattern every queue read uses: filter on (company, status),
order by txn_date. Created concurrently is not used because Alembic runs the
migration inside a transaction on startup; the table is small enough that a
brief lock is not a concern at current volumes.

Revision ID: b1c2d3e4f5a6
Revises: 32974f0dc25d
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, Sequence[str], None] = '32974f0dc25d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_charges_company_status_date", "charges",
                    ["company", "status", "txn_date"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_charges_company_status_date", table_name="charges")
