"""audit log company+at index

The audit log is read newest-first per company and grows faster than the
charges table. Without an index the ORDER BY sorted every row for the company
on each read.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'c2d3e4f5a6b7'
down_revision: Union[str, Sequence[str], None] = 'b1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_audit_company_at", "audit_log", ["company", "at"],
                    unique=False)


def downgrade() -> None:
    op.drop_index("ix_audit_company_at", table_name="audit_log")
