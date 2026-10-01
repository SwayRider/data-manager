"""region_gtfs_feed

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-30 23:59:30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0014'
down_revision: Union[str, None] = '0013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('region_gtfs_feed',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('region_id', sa.Integer(), nullable=False),
    sa.Column('url', sa.String(length=500), nullable=False),
    sa.Column('label', sa.String(length=100), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['region_id'], ['region.id'], name=op.f('fk_region_gtfs_feed_region_id_region'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_region_gtfs_feed')),
    sa.UniqueConstraint('region_id', 'url', name=op.f('uq_region_gtfs_feed_region_id'))
    )


def downgrade() -> None:
    op.drop_table('region_gtfs_feed')
