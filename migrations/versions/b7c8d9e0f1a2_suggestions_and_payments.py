"""stored suggestions and the card-payment flag

The Vendor and Category columns on For Review show the engine's proposal, which
was computed per request and so could not be filtered or sorted. Stored now, the
same way tier and score were.

is_card_payment separates money returned by a merchant (a refund) from money
paid onto the card (a payment) -- distinct to an accountant, and only derivable
from the description.

Revision ID: b7c8d9e0f1a2
Revises: a6b7c8d9e0f1
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b7c8d9e0f1a2'
down_revision: Union[str, Sequence[str], None] = 'a6b7c8d9e0f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("charges", sa.Column("suggested_vendor", sa.String(),
                                       nullable=False, server_default=""))
    op.add_column("charges", sa.Column("suggested_category", sa.String(),
                                       nullable=False, server_default=""))
    op.add_column("charges", sa.Column("is_card_payment", sa.Boolean(),
                                       nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("charges", "is_card_payment")
    op.drop_column("charges", "suggested_category")
    op.drop_column("charges", "suggested_vendor")
