"""Requirement history: one row per change to a requirement, with each field's value before and after

Revision ID: 0004
Revises: 0002
Create Date: 2026-10-30 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import archflow.db.types

revision = '0004'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Like the baseline, skip what exists: a database without an Alembic stamp may already have the table.
    if 'requirement_revisions' in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        'requirement_revisions',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('project_id', sa.Text(), nullable=False),
        sa.Column('branch', sa.Text(), nullable=False),
        sa.Column('requirement_id', sa.Text(), nullable=False),
        sa.Column('rev', sa.Integer(), nullable=False),
        sa.Column('action', sa.Text(), nullable=False),
        sa.Column('actor', sa.Text(), nullable=False),
        sa.Column('changes', archflow.db.types.JSONType, nullable=False),
        sa.Column('created_at', archflow.db.types.UTCDateTime(), nullable=False),
        sa.CheckConstraint("action IN ('added', 'updated', 'deleted', 'restored', 'purged')",
                           name='ck_requirement_revisions_action'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_requirement_revisions', 'requirement_revisions',
                    ['project_id', 'branch', 'requirement_id', sa.text('id DESC')])


def downgrade() -> None:
    op.drop_index('idx_requirement_revisions', table_name='requirement_revisions')
    op.drop_table('requirement_revisions')
