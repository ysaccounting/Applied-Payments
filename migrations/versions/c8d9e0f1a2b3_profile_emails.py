"""cardholder name -> email, from the Airtable address book

Only the Divvy export carries an email address. Slash and WEX name the
cardholder but not their email, so the scoring engine's email signal could never
fire for them. Mirroring the address book supplies it for every program.

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c8d9e0f1a2b3'
down_revision: Union[str, Sequence[str], None] = 'b7c8d9e0f1a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "profile_emails",
        sa.Column("name_key", sa.String(), primary_key=True),
        sa.Column("display_name", sa.String(), nullable=False, server_default=""),
        sa.Column("email", sa.String(), nullable=False, server_default=""),
        sa.Column("synced_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_profile_emails_email", "profile_emails", ["email"])


def downgrade() -> None:
    op.drop_index("ix_profile_emails_email", table_name="profile_emails")
    op.drop_table("profile_emails")
