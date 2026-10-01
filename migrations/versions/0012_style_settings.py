"""style_settings

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-30 23:30:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0012'
down_revision: Union[str, None] = '0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('style_settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('config_profile_id', sa.Integer(), nullable=False),
    sa.Column('light_style', sa.String(length=40), nullable=False),
    sa.Column('dark_style', sa.String(length=40), nullable=False),
    sa.Column('labels_json', sa.JSON(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['config_profile_id'], ['config_profile.id'], name=op.f('fk_style_settings_config_profile_id_config_profile'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_style_settings')),
    sa.UniqueConstraint('config_profile_id', name=op.f('uq_style_settings_config_profile_id'))
    )


def downgrade() -> None:
    op.drop_table('style_settings')
