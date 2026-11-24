"""Jira wake-up: webhook URL id and its secret (encrypted)

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-24 14:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0015'
down_revision: str | None = '0014'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('user_settings', sa.Column('jira_hook_id', sa.String(length=48), nullable=True))
    op.add_column('user_settings', sa.Column('jira_hook_secret_encrypted', sa.Text(), nullable=True))
    op.create_index('ix_user_settings_jira_hook_id', 'user_settings', ['jira_hook_id'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_user_settings_jira_hook_id', table_name='user_settings')
    op.drop_column('user_settings', 'jira_hook_secret_encrypted')
    op.drop_column('user_settings', 'jira_hook_id')
