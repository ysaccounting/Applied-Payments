"""track a cleared suggestion per field

One flag for the whole charge meant clearing the vendor also blanked the
category, and setting either one back restored both.

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e6f7a8b9c0d1'
down_revision: Union[str, Sequence[str], None] = 'd5e6f7a8b9c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("charges", sa.Column("vendor_cleared", sa.Boolean(),
                                       nullable=False, server_default=sa.false()))
    op.add_column("charges", sa.Column("category_cleared", sa.Boolean(),
                                       nullable=False, server_default=sa.false()))
    # Carry the old single flag over to both, which is what it meant.
    op.execute("UPDATE charges SET vendor_cleared = suggestion_cleared, "
               "category_cleared = suggestion_cleared")


def downgrade() -> None:
    op.drop_column("charges", "category_cleared")
    op.drop_column("charges", "vendor_cleared")
