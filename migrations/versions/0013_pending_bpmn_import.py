"""a BPMN file kept on the server between the Create page and the choice page

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-24 00:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0013'
down_revision: str | None = '0012'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'pending_bpmn_imports',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('xml', sa.LargeBinary(), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('context', sa.Text(), nullable=False),
        sa.Column('shared', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_pending_bpmn_imports_user_id', 'pending_bpmn_imports', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_pending_bpmn_imports_user_id', table_name='pending_bpmn_imports')
    op.drop_table('pending_bpmn_imports')
