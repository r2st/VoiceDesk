"""Make the analytics rollup scope genuinely unique

``UNIQUE(business_id, agent_id, date)`` does not constrain the business-wide
roll-up at all: ``agent_id`` is NULL there, and SQL treats NULLs as distinct,
so two rollup runs racing for the same tenant and day each insert their own
row and every dashboard read after that is doubled. It also permanently burns
the (tenant, agent, day) triple once a row is soft-deleted by retention.

Replaced with two partial unique indexes — one for the agent-scoped rows, one
for the business-wide row — both predicated on ``deleted_at IS NULL``.

Revision ID: b7e2c0a91f34
Revises: 30bfaf8254c3
Create Date: 2026-08-09

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b7e2c0a91f34"
down_revision = "30bfaf8254c3"
branch_labels = None
depends_on = None

AGENT_SCOPED = sa.text("agent_id IS NOT NULL AND deleted_at IS NULL")
BUSINESS_WIDE = sa.text("agent_id IS NULL AND deleted_at IS NULL")


def upgrade() -> None:
    # Collapse any duplicate business-wide rows the old constraint allowed
    # through, keeping the most recently written one.
    op.execute(
        sa.text(
            """
            DELETE FROM analytics a
            USING analytics b
            WHERE a.business_id = b.business_id
              AND a.date = b.date
              AND a.agent_id IS NULL
              AND b.agent_id IS NULL
              AND a.deleted_at IS NULL
              AND b.deleted_at IS NULL
              AND (a.updated_at, a.id) < (b.updated_at, b.id)
            """
        )
    )
    op.drop_constraint("uq_analytics_scope_date", "analytics", type_="unique")
    op.create_index(
        "uq_analytics_agent_date_active",
        "analytics",
        ["business_id", "agent_id", "date"],
        unique=True,
        postgresql_where=AGENT_SCOPED,
    )
    op.create_index(
        "uq_analytics_business_date_active",
        "analytics",
        ["business_id", "date"],
        unique=True,
        postgresql_where=BUSINESS_WIDE,
    )


def downgrade() -> None:
    op.drop_index("uq_analytics_business_date_active", table_name="analytics")
    op.drop_index("uq_analytics_agent_date_active", table_name="analytics")
    op.create_unique_constraint(
        "uq_analytics_scope_date", "analytics", ["business_id", "agent_id", "date"]
    )
