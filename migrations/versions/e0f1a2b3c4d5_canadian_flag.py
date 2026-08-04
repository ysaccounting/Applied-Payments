"""mark charges made in Canada

No export carries a currency or country field, so this is inferred from the
bank detail and stored alongside the other derived flags.

Revision ID: e0f1a2b3c4d5
Revises: d9e0f1a2b3c4
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e0f1a2b3c4d5'
down_revision: Union[str, Sequence[str], None] = 'd9e0f1a2b3c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("charges", sa.Column("is_canadian", sa.Boolean(),
                                       nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("charges", "is_canadian")
