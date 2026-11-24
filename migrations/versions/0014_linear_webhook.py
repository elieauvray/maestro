"""Linear wake-up: webhook URL id and the signing secret Linear gives (encrypted)

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-24 12:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0014'
down_revision: str | None = '0013'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('user_settings', sa.Column('linear_hook_id', sa.String(length=48), nullable=True))
    op.add_column('user_settings', sa.Column('linear_hook_secret_encrypted', sa.Text(), nullable=True))
    op.create_index('ix_user_settings_linear_hook_id', 'user_settings', ['linear_hook_id'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_user_settings_linear_hook_id', table_name='user_settings')
    op.drop_column('user_settings', 'linear_hook_secret_encrypted')
    op.drop_column('user_settings', 'linear_hook_id')
