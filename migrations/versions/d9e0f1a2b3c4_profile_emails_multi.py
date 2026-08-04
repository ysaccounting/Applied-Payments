"""a profile can hold several email addresses

One row per (name, address) instead of one per name. Profiles routinely buy
under more than one address -- Kayla Bentley uses both a gmail and a work
address -- and a bill can name any of them, so keeping only the primary missed
the rest.

Recreated rather than altered: the table is a mirror, rebuilt from Airtable on
the next sync, so there is nothing to preserve.

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd9e0f1a2b3c4'
down_revision: Union[str, Sequence[str], None] = 'c8d9e0f1a2b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_profile_emails_email", table_name="profile_emails")
    op.drop_table("profile_emails")
    op.create_table(
        "profile_emails",
        sa.Column("name_key", sa.String(), primary_key=True),
        sa.Column("email", sa.String(), primary_key=True),
        sa.Column("display_name", sa.String(), nullable=False, server_default=""),
        sa.Column("synced_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_profile_emails_email", "profile_emails", ["email"])


def downgrade() -> None:
    op.drop_index("ix_profile_emails_email", table_name="profile_emails")
    op.drop_table("profile_emails")
    op.create_table(
        "profile_emails",
        sa.Column("name_key", sa.String(), primary_key=True),
        sa.Column("display_name", sa.String(), nullable=False, server_default=""),
        sa.Column("email", sa.String(), nullable=False, server_default=""),
        sa.Column("synced_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_profile_emails_email", "profile_emails", ["email"])
