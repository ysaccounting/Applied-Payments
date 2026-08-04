"""remember a deliberately cleared suggestion

Clearing the Vendor or Category on an unresolved charge blanked the stored
suggestion, but the next scoring run recomputed and restored it -- so the clear
undid itself within five minutes. This flag makes the choice stick.

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a2b3c4d5e6f7'
down_revision: Union[str, Sequence[str], None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("charges", sa.Column("suggestion_cleared", sa.Boolean(),
                                       nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("charges", "suggestion_cleared")
