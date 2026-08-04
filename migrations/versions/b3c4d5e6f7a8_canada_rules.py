"""Canada rules as data

The teams, venues and city/province pairs that mark a charge as Canadian were a
hardcoded list. The list is never finished -- a new venue or a renamed merchant
should not need a deploy -- so it becomes a table, seeded from the original.

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b3c4d5e6f7a8'
down_revision: Union[str, Sequence[str], None] = 'a2b3c4d5e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "canada_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("phrase", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False, server_default="phrase"),
        sa.Column("provinces", sa.String(), nullable=False, server_default=""),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("note", sa.String(), nullable=False, server_default=""),
    )
    op.create_index("ix_canada_rules_phrase", "canada_rules", ["phrase"])

    # Seed from the list that was in code, so nothing regresses on deploy.
    from app.canada import _CANADIAN_ENTITIES, _CITY_PROVINCE
    rows = [{"phrase": p, "kind": "phrase", "provinces": "", "active": True,
             "note": "seeded"} for p in _CANADIAN_ENTITIES]
    rows += [{"phrase": city, "kind": "city_province",
              "provinces": ",".join(sorted(provs)), "active": True,
              "note": "seeded"} for city, provs in _CITY_PROVINCE.items()]
    if rows:
        op.bulk_insert(sa.table(
            "canada_rules",
            sa.column("phrase", sa.String), sa.column("kind", sa.String),
            sa.column("provinces", sa.String), sa.column("active", sa.Boolean),
            sa.column("note", sa.String)), rows)


def downgrade() -> None:
    op.drop_index("ix_canada_rules_phrase", table_name="canada_rules")
    op.drop_table("canada_rules")
