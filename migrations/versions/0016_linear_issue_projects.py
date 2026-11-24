"""remember which Linear project each issue belongs to (comments only carry the issue id)

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-24 16:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0016'
down_revision: str | None = '0015'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'linear_issue_projects',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('issue_id', sa.String(length=64), nullable=False),
        sa.Column('project_hints', sa.JSON(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('user_id', 'issue_id', name='uq_linear_issue_project'),
    )


def downgrade() -> None:
    op.drop_table('linear_issue_projects')
