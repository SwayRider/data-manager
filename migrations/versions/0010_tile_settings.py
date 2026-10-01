"""tile_settings, tile_override, tile_source_override

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-30 22:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0010'
down_revision: Union[str, None] = '0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('tile_settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('config_profile_id', sa.Integer(), nullable=False),
    sa.Column('tile_size', sa.Integer(), nullable=False),
    sa.Column('min_lat', sa.Integer(), nullable=True),
    sa.Column('max_lat', sa.Integer(), nullable=True),
    sa.Column('min_lon', sa.Integer(), nullable=True),
    sa.Column('max_lon', sa.Integer(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['config_profile_id'], ['config_profile.id'], name=op.f('fk_tile_settings_config_profile_id_config_profile'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tile_settings')),
    sa.UniqueConstraint('config_profile_id', name=op.f('uq_tile_settings_config_profile_id'))
    )
    op.create_table('tile_override',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('config_profile_id', sa.Integer(), nullable=False),
    sa.Column('tile_id', sa.String(length=12), nullable=False),
    sa.Column('mode', sa.String(length=10), nullable=False),
    sa.ForeignKeyConstraint(['config_profile_id'], ['config_profile.id'], name=op.f('fk_tile_override_config_profile_id_config_profile'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tile_override')),
    sa.UniqueConstraint('config_profile_id', 'tile_id', name=op.f('uq_tile_override_config_profile_id'))
    )
    op.create_table('tile_source_override',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('config_profile_id', sa.Integer(), nullable=False),
    sa.Column('path', sa.String(length=200), nullable=False),
    sa.Column('mode', sa.String(length=10), nullable=False),
    sa.ForeignKeyConstraint(['config_profile_id'], ['config_profile.id'], name=op.f('fk_tile_source_override_config_profile_id_config_profile'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tile_source_override')),
    sa.UniqueConstraint('config_profile_id', 'path', name=op.f('uq_tile_source_override_config_profile_id'))
    )


def downgrade() -> None:
    op.drop_table('tile_source_override')
    op.drop_table('tile_override')
    op.drop_table('tile_settings')
