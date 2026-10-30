"""Comments (threads and replies on components, decisions and projects) and mention/reply notifications

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-26 13:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import archflow.db.types

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Like the baseline, skip what exists: a database without an Alembic stamp may already have these tables.
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if 'comments' not in existing:
        _comments()
    if 'notifications' not in existing:
        _notifications()


def _comments() -> None:
    op.create_table(
        'comments',
        sa.Column('id', sa.Text(), nullable=False),
        sa.Column('project_id', sa.Text(), nullable=False),
        sa.Column('branch', sa.Text(), nullable=False),
        sa.Column('target_type', sa.Text(), server_default='project', nullable=False),
        sa.Column('target_id', sa.Text(), server_default='', nullable=False),
        sa.Column('parent_id', sa.Text(), server_default='', nullable=False),
        sa.Column('author_id', sa.Text(), nullable=False),
        sa.Column('author_name', sa.Text(), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('mentions', archflow.db.types.JSONType, server_default='[]', nullable=False),
        sa.Column('resolved', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('resolved_by', sa.Text(), server_default='', nullable=False),
        sa.Column('created_at', archflow.db.types.UTCDateTime(), nullable=False),
        sa.Column('edited_at', archflow.db.types.UTCDateTime(), nullable=True),
        sa.CheckConstraint("target_type IN ('project', 'node', 'decision')", name='ck_comments_target'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_comments_project', 'comments', ['project_id', 'branch', 'created_at'])
    op.create_index('idx_comments_parent', 'comments', ['parent_id'])


def _notifications() -> None:
    op.create_table(
        'notifications',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Text(), nullable=False),
        sa.Column('project_id', sa.Text(), nullable=False),
        sa.Column('branch', sa.Text(), nullable=False),
        sa.Column('comment_id', sa.Text(), nullable=False),
        sa.Column('thread_id', sa.Text(), nullable=False),
        sa.Column('kind', sa.Text(), nullable=False),
        sa.Column('actor_name', sa.Text(), nullable=False),
        sa.Column('preview', sa.Text(), server_default='', nullable=False),
        sa.Column('read', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('created_at', archflow.db.types.UTCDateTime(), nullable=False),
        sa.CheckConstraint("kind IN ('mention', 'reply')", name='ck_notifications_kind'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['comment_id'], ['comments.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_notifications_user', 'notifications', ['user_id', sa.text('created_at DESC')])


def downgrade() -> None:
    op.drop_index('idx_notifications_user', table_name='notifications')
    op.drop_table('notifications')
    op.drop_index('idx_comments_parent', table_name='comments')
    op.drop_index('idx_comments_project', table_name='comments')
    op.drop_table('comments')
