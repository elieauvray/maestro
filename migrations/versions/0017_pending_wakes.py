"""wake-ups that arrive during a run are queued; the tracker account Maestro acts with is recognized

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-24 18:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0017'
down_revision: str | None = '0016'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('workflows', sa.Column('wake_pending', sa.Text(), nullable=True))
    op.add_column('workflows', sa.Column('wake_pending_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('user_settings', sa.Column('linear_self_actor', sa.String(length=128), nullable=True))
    op.add_column('user_settings', sa.Column('jira_self_actor', sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column('user_settings', 'jira_self_actor')
    op.drop_column('user_settings', 'linear_self_actor')
    op.drop_column('workflows', 'wake_pending_at')
    op.drop_column('workflows', 'wake_pending')
