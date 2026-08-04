"""matched bill snapshot on charges

Captures the bill's number, vendor, date and memo on the charge when a match is
recorded. Necessary because sync_bills deletes any bill QuickBooks no longer
returns as open, and paying a bill is what makes it stop being open -- so the
BillRow disappears within one sync of the match that referenced it.

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd3e4f5a6b7c8'
down_revision: Union[str, Sequence[str], None] = 'c2d3e4f5a6b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLS = [
    ("matched_bill_no", sa.String()),
    ("matched_bill_vendor", sa.String()),
    ("matched_bill_date", sa.String()),
    ("matched_bill_memo", sa.Text()),
]


def upgrade() -> None:
    for name, type_ in _COLS:
        op.add_column("charges",
                      sa.Column(name, type_, nullable=False, server_default=""))


def downgrade() -> None:
    for name, _ in _COLS:
        op.drop_column("charges", name)
