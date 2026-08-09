"""Add the call_takeovers table

Live call monitoring (design doc §4.3) lets a supervisor take a call over from
the AI mid-conversation. The open row for a call — ``ended_at IS NULL`` — is the
current controller, and the partial unique index makes two simultaneous
controllers impossible rather than merely unlikely. Closed rows stay as the
audit trail of who joined which call.

Revision ID: 9b3e0f1c74a2
Revises: 5d67214240a0
Create Date: 2026-08-09 09:40:12.114503
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.types  # noqa: F401  (custom GUID / JSONBType columns)

revision: str = '9b3e0f1c74a2'
down_revision: str | None = '5d67214240a0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'call_takeovers',
        sa.Column('business_id', app.db.types.GUID(), nullable=False),
        sa.Column('call_id', app.db.types.GUID(), nullable=False),
        sa.Column('supervisor_user_id', app.db.types.GUID(), nullable=False),
        sa.Column('reason', sa.String(length=200), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('turns_spoken', sa.Integer(), nullable=False),
        sa.Column('returned_to_ai', sa.Boolean(), nullable=False),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ['business_id'],
            ['businesses.id'],
            name=op.f('fk_call_takeovers_business_id_businesses'),
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['call_id'],
            ['call_logs.id'],
            name=op.f('fk_call_takeovers_call_id_call_logs'),
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['supervisor_user_id'],
            ['users.id'],
            name=op.f('fk_call_takeovers_supervisor_user_id_users'),
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_call_takeovers')),
    )
    op.create_index(
        op.f('ix_call_takeovers_business_id'), 'call_takeovers', ['business_id'], unique=False
    )
    op.create_index(op.f('ix_call_takeovers_call_id'), 'call_takeovers', ['call_id'], unique=False)
    op.create_index(
        op.f('ix_call_takeovers_supervisor_user_id'),
        'call_takeovers',
        ['supervisor_user_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_call_takeovers_deleted_at'), 'call_takeovers', ['deleted_at'], unique=False
    )
    op.create_index(
        'ix_call_takeovers_business_started',
        'call_takeovers',
        ['business_id', 'started_at'],
        unique=False,
    )
    op.create_index(
        'uq_call_takeovers_active_call',
        'call_takeovers',
        ['call_id'],
        unique=True,
        postgresql_where=sa.text('ended_at IS NULL AND deleted_at IS NULL'),
        sqlite_where=sa.text('ended_at IS NULL AND deleted_at IS NULL'),
    )


def downgrade() -> None:
    op.drop_index('uq_call_takeovers_active_call', table_name='call_takeovers')
    op.drop_index('ix_call_takeovers_business_started', table_name='call_takeovers')
    op.drop_index(op.f('ix_call_takeovers_deleted_at'), table_name='call_takeovers')
    op.drop_index(op.f('ix_call_takeovers_supervisor_user_id'), table_name='call_takeovers')
    op.drop_index(op.f('ix_call_takeovers_call_id'), table_name='call_takeovers')
    op.drop_index(op.f('ix_call_takeovers_business_id'), table_name='call_takeovers')
    op.drop_table('call_takeovers')
