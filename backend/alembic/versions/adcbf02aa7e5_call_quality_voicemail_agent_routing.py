"""Call quality metrics, voicemail, and agent routing capacity

Three additions in one migration since they share nothing but a release:

* ``call_quality_metrics`` — append-only latency/jitter/packet-loss samples
  the media edge posts while a call is live (design doc call-quality work).
* ``voicemails`` — messages captured when no agent is available, encrypted at
  rest the same way call recordings are.
* ``voice_agents.max_concurrent_calls`` / ``voicemail_enabled`` — the
  concurrency limit and voicemail opt-in that inbound routing reads to decide
  whether an agent can take the next call.

Revision ID: adcbf02aa7e5
Revises: b7e2c0a91f34
Create Date: 2026-08-10 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.types  # noqa: F401  (custom GUID / JSONBType columns)

revision: str = 'adcbf02aa7e5'
down_revision: str | None = 'b7e2c0a91f34'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'call_quality_metrics',
        sa.Column('business_id', app.db.types.GUID(), nullable=False),
        sa.Column('call_id', app.db.types.GUID(), nullable=False),
        sa.Column('sampled_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('latency_ms', sa.Integer(), nullable=False),
        sa.Column('jitter_ms', sa.Float(), nullable=False),
        sa.Column('packet_loss_pct', sa.Float(), nullable=False),
        sa.Column('mos_score', sa.Float(), nullable=True),
        sa.Column('grade', sa.String(length=10), nullable=False),
        sa.Column('source', sa.String(length=40), nullable=False),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ['business_id'],
            ['businesses.id'],
            name=op.f('fk_call_quality_metrics_business_id_businesses'),
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['call_id'],
            ['call_logs.id'],
            name=op.f('fk_call_quality_metrics_call_id_call_logs'),
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_call_quality_metrics')),
    )
    op.create_index(
        op.f('ix_call_quality_metrics_business_id'),
        'call_quality_metrics',
        ['business_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_call_quality_metrics_call_id'), 'call_quality_metrics', ['call_id'], unique=False
    )
    op.create_index(
        'ix_call_quality_metrics_call_sampled',
        'call_quality_metrics',
        ['call_id', 'sampled_at'],
        unique=False,
    )
    op.create_index(
        'ix_call_quality_metrics_business_created',
        'call_quality_metrics',
        ['business_id', 'created_at'],
        unique=False,
    )

    op.create_table(
        'voicemails',
        sa.Column('business_id', app.db.types.GUID(), nullable=False),
        sa.Column('call_id', app.db.types.GUID(), nullable=False),
        sa.Column('phone_number_id', app.db.types.GUID(), nullable=True),
        sa.Column('caller_number', sa.String(length=20), nullable=False),
        sa.Column('storage_path', sa.String(length=500), nullable=False),
        sa.Column('storage_bucket', sa.String(length=120), nullable=False),
        sa.Column('content_type', sa.String(length=60), nullable=False),
        sa.Column('format', sa.String(length=20), nullable=False),
        sa.Column('duration_sec', sa.Integer(), nullable=False),
        sa.Column('size_bytes', sa.BigInteger(), nullable=False),
        sa.Column('checksum_sha256', sa.String(length=64), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('transcript', sa.Text(), nullable=True),
        sa.Column('transcribed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('listened_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('listened_by', app.db.types.GUID(), nullable=True),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ['business_id'],
            ['businesses.id'],
            name=op.f('fk_voicemails_business_id_businesses'),
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['call_id'],
            ['call_logs.id'],
            name=op.f('fk_voicemails_call_id_call_logs'),
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['phone_number_id'],
            ['phone_numbers.id'],
            name=op.f('fk_voicemails_phone_number_id_phone_numbers'),
            ondelete='SET NULL',
        ),
        sa.ForeignKeyConstraint(
            ['listened_by'],
            ['users.id'],
            name=op.f('fk_voicemails_listened_by_users'),
            ondelete='SET NULL',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_voicemails')),
        sa.UniqueConstraint('call_id', name=op.f('uq_voicemails_call_id')),
    )
    op.create_index(op.f('ix_voicemails_business_id'), 'voicemails', ['business_id'], unique=False)
    op.create_index(
        op.f('ix_voicemails_caller_number'), 'voicemails', ['caller_number'], unique=False
    )
    op.create_index(op.f('ix_voicemails_deleted_at'), 'voicemails', ['deleted_at'], unique=False)
    op.create_index(
        'ix_voicemails_business_created', 'voicemails', ['business_id', 'created_at'], unique=False
    )

    op.add_column(
        'voice_agents',
        sa.Column(
            'max_concurrent_calls', sa.Integer(), server_default='5', nullable=False
        ),
    )
    op.add_column(
        'voice_agents',
        sa.Column(
            'voicemail_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column('voice_agents', 'voicemail_enabled')
    op.drop_column('voice_agents', 'max_concurrent_calls')

    op.drop_index('ix_voicemails_business_created', table_name='voicemails')
    op.drop_index(op.f('ix_voicemails_deleted_at'), table_name='voicemails')
    op.drop_index(op.f('ix_voicemails_caller_number'), table_name='voicemails')
    op.drop_index(op.f('ix_voicemails_business_id'), table_name='voicemails')
    op.drop_table('voicemails')

    op.drop_index('ix_call_quality_metrics_business_created', table_name='call_quality_metrics')
    op.drop_index('ix_call_quality_metrics_call_sampled', table_name='call_quality_metrics')
    op.drop_index(op.f('ix_call_quality_metrics_call_id'), table_name='call_quality_metrics')
    op.drop_index(op.f('ix_call_quality_metrics_business_id'), table_name='call_quality_metrics')
    op.drop_table('call_quality_metrics')
