"""region

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-30 18:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0006'
down_revision: Union[str, None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('region',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('config_profile_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100, collation='NOCASE'), nullable=False),
    sa.Column('color', sa.String(length=7), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['config_profile_id'], ['config_profile.id'], name=op.f('fk_region_config_profile_id_config_profile'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_region')),
    sa.UniqueConstraint('config_profile_id', 'name', name=op.f('uq_region_config_profile_id'))
    )
    op.create_table('region_country',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('region_id', sa.Integer(), nullable=False),
    sa.Column('config_profile_id', sa.Integer(), nullable=False),
    sa.Column('country_iso', sa.String(length=10), nullable=False),
    sa.Column('role', sa.String(length=10), nullable=False),
    sa.ForeignKeyConstraint(['config_profile_id'], ['config_profile.id'], name=op.f('fk_region_country_config_profile_id_config_profile'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['country_iso'], ['country.iso2'], name=op.f('fk_region_country_country_iso_country'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['region_id'], ['region.id'], name=op.f('fk_region_country_region_id_region'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_region_country')),
    sa.UniqueConstraint('config_profile_id', 'country_iso', name=op.f('uq_region_country_config_profile_id'))
    )


def downgrade() -> None:
    op.drop_table('region_country')
    op.drop_table('region')
