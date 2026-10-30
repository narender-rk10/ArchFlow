"""Version approvals (review actions per saved version), each org's approval policy, and review notifications

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-29 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import archflow.db.types

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None

OLD_KINDS = "kind IN ('mention', 'reply')"
NEW_KINDS = "kind IN ('mention', 'reply', 'review_requested', 'approved', 'changes_requested')"


def upgrade() -> None:
    # Like the baseline, skip what exists: a database without an Alembic stamp may already have these tables.
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())
    if 'approvals' not in existing:
        _approvals()
    if 'org_approval_policy' not in existing:
        _policy()
    if 'version' not in {c['name'] for c in inspector.get_columns('notifications')}:
        _review_notifications()


def _approvals() -> None:
    op.create_table(
        'approvals',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('project_id', sa.Text(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('branch', sa.Text(), nullable=False),
        sa.Column('action', sa.Text(), nullable=False),
        sa.Column('actor_id', sa.Text(), nullable=False),
        sa.Column('actor_name', sa.Text(), nullable=False),
        sa.Column('comment', sa.Text(), server_default='', nullable=False),
        sa.Column('created_at', archflow.db.types.UTCDateTime(), nullable=False),
        sa.CheckConstraint("action IN ('request', 'approve', 'request_changes')", name='ck_approvals_action'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_approvals_version', 'approvals', ['project_id', 'version'])


def _policy() -> None:
    op.create_table(
        'org_approval_policy',
        sa.Column('org_id', sa.Text(), nullable=False),
        sa.Column('required_approvals', sa.Integer(), server_default='1', nullable=False),
        sa.Column('require_for_adopt', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('updated_by', sa.Text(), nullable=True),
        sa.Column('updated_at', archflow.db.types.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(['org_id'], ['orgs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('org_id'),
    )


def _review_notifications() -> None:
    # Review notifications point at a version, not a comment. SQLite cannot alter a column or a CHECK in place, so
    # batch mode rebuilds the table (Postgres alters it).
    with op.batch_alter_table('notifications', recreate='auto') as batch:
        batch.add_column(sa.Column('version', sa.Integer(), nullable=True))
        batch.alter_column('comment_id', existing_type=sa.Text(), nullable=True)
        batch.drop_constraint('ck_notifications_kind', type_='check')
        batch.create_check_constraint('ck_notifications_kind', NEW_KINDS)


def downgrade() -> None:
    op.execute("DELETE FROM notifications WHERE comment_id IS NULL")
    with op.batch_alter_table('notifications', recreate='auto') as batch:
        batch.drop_constraint('ck_notifications_kind', type_='check')
        batch.create_check_constraint('ck_notifications_kind', OLD_KINDS)
        batch.alter_column('comment_id', existing_type=sa.Text(), nullable=False)
        batch.drop_column('version')
    op.drop_table('org_approval_policy')
    op.drop_index('idx_approvals_version', table_name='approvals')
    op.drop_table('approvals')
