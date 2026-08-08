"""Make natural-key uniqueness ignore soft-deleted rows

Rows are never physically deleted (design doc §3.1), so a plain UNIQUE
constraint permanently burns the value it covers: once an agent is deleted its
name can never be used again, and a released phone number can never be
re-provisioned. These constraints become partial unique indexes predicated on
``deleted_at IS NULL``.

Revision ID: 8c1f47b920de
Revises: 42ad54f988ea
Create Date: 2026-08-08

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "8c1f47b920de"
down_revision = "42ad54f988ea"
branch_labels = None
depends_on = None

ACTIVE = sa.text("deleted_at IS NULL")

#: (table, old constraint name, new index name, columns)
CONVERSIONS = [
    (
        "voice_agents",
        "uq_voice_agents_business_id_name",
        "uq_voice_agents_business_name_active",
        ["business_id", "name"],
    ),
    (
        "users",
        "uq_users_business_id_email",
        "uq_users_business_email_active",
        ["business_id", "email"],
    ),
    (
        "intents",
        "uq_intents_scope_name",
        "uq_intents_scope_name_active",
        ["business_id", "agent_id", "name"],
    ),
    (
        "phone_numbers",
        "uq_phone_numbers_number_provider",
        "uq_phone_numbers_number_provider_active",
        ["number", "provider"],
    ),
]


def upgrade() -> None:
    for table, constraint, index, columns in CONVERSIONS:
        op.drop_constraint(constraint, table, type_="unique")
        op.create_index(index, table, columns, unique=True, postgresql_where=ACTIVE)


def downgrade() -> None:
    for table, constraint, index, columns in CONVERSIONS:
        op.drop_index(index, table_name=table)
        op.create_unique_constraint(constraint, table, columns)
