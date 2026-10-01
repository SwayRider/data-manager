"""region_overlap_override

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-30 19:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0007'
down_revision: Union[str, None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('region_overlap_override',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('region_id', sa.Integer(), nullable=False),
    sa.Column('country_iso', sa.String(length=10), nullable=False),
    sa.Column('mode', sa.String(length=10), nullable=False),
    sa.ForeignKeyConstraint(['country_iso'], ['country.iso2'], name=op.f('fk_region_overlap_override_country_iso_country'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['region_id'], ['region.id'], name=op.f('fk_region_overlap_override_region_id_region'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_region_overlap_override')),
    sa.UniqueConstraint('region_id', 'country_iso', name=op.f('uq_region_overlap_override_region_id'))
    )


def downgrade() -> None:
    op.drop_table('region_overlap_override')
