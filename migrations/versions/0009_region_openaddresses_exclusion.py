"""region_openaddresses_exclusion

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-30 21:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0009'
down_revision: Union[str, None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('region_openaddresses_exclusion',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('region_id', sa.Integer(), nullable=False),
    sa.Column('country_iso', sa.String(length=10), nullable=False),
    sa.Column('file', sa.String(length=300), nullable=False),
    sa.ForeignKeyConstraint(['country_iso'], ['country.iso2'], name=op.f('fk_region_openaddresses_exclusion_country_iso_country'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['region_id'], ['region.id'], name=op.f('fk_region_openaddresses_exclusion_region_id_region'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_region_openaddresses_exclusion')),
    sa.UniqueConstraint('region_id', 'country_iso', 'file', name=op.f('uq_region_openaddresses_exclusion_region_id'))
    )


def downgrade() -> None:
    op.drop_table('region_openaddresses_exclusion')
