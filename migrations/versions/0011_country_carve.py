"""country_carve

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-30 23:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0011'
down_revision: Union[str, None] = '0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('country_carve',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('config_profile_id', sa.Integer(), nullable=False),
    sa.Column('country_iso', sa.String(length=10), nullable=False),
    sa.Column('keep_geojson', sa.JSON(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['config_profile_id'], ['config_profile.id'], name=op.f('fk_country_carve_config_profile_id_config_profile'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['country_iso'], ['country.iso2'], name=op.f('fk_country_carve_country_iso_country'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_country_carve')),
    sa.UniqueConstraint('config_profile_id', 'country_iso', name=op.f('uq_country_carve_config_profile_id'))
    )


def downgrade() -> None:
    op.drop_table('country_carve')
