"""Jira Cloud push: an org's Jira connection (encrypted API token) and links from ArchFlow objects to issues

Revision ID: 0005
Revises: 0002
Create Date: 2026-10-30 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import archflow.db.types

revision = '0005'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Like the baseline, skip what exists: a database without an Alembic stamp may already have these tables.
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if 'org_jira_connections' not in existing:
        _org_jira_connections()
    if 'external_links' not in existing:
        _external_links()


def _org_jira_connections() -> None:
    op.create_table(
        'org_jira_connections',
        sa.Column('org_id', sa.Text(), nullable=False),
        sa.Column('site_url', sa.Text(), nullable=False),
        sa.Column('email', sa.Text(), nullable=False),
        sa.Column('ciphertext', sa.Text(), nullable=False),
        sa.Column('last4', sa.Text(), nullable=False),
        sa.Column('project_key', sa.Text(), server_default='', nullable=False),
        sa.Column('updated_by', sa.Text(), nullable=True),
        sa.Column('updated_at', archflow.db.types.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(['org_id'], ['orgs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('org_id'),
    )


def _external_links() -> None:
    op.create_table(
        'external_links',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('project_id', sa.Text(), nullable=False),
        sa.Column('system', sa.Text(), server_default='jira', nullable=False),
        sa.Column('object_kind', sa.Text(), nullable=False),
        sa.Column('object_id', sa.Text(), nullable=False),
        sa.Column('external_key', sa.Text(), nullable=False),
        sa.Column('external_url', sa.Text(), server_default='', nullable=False),
        sa.Column('content_hash', sa.Text(), server_default='', nullable=False),
        sa.Column('status', sa.Text(), server_default='', nullable=False),
        sa.Column('status_category', sa.Text(), server_default='', nullable=False),
        sa.Column('status_checked_at', archflow.db.types.UTCDateTime(), nullable=True),
        sa.Column('updated_at', archflow.db.types.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('project_id', 'system', 'object_kind', 'object_id', name='uq_external_links_object'),
    )
    op.create_index('idx_external_links_key', 'external_links', ['system', 'external_key'])


def downgrade() -> None:
    op.drop_index('idx_external_links_key', table_name='external_links')
    op.drop_table('external_links')
    op.drop_table('org_jira_connections')
