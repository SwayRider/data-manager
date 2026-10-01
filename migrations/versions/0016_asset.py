"""asset

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-01 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0016'
down_revision: Union[str, None] = '0015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('asset',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('asset_type', sa.String(length=50), nullable=False),
    sa.Column('name', sa.String(length=150), nullable=False),
    sa.Column('config_profile_id', sa.Integer(), nullable=True),
    sa.Column('produced_by_run_id', sa.Integer(), nullable=True),
    sa.Column('path', sa.String(length=500), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('meta_json', sa.JSON(), nullable=False),
    sa.Column('source_download_ids', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['config_profile_id'], ['config_profile.id'], name=op.f('fk_asset_config_profile_id_config_profile'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['produced_by_run_id'], ['build_run.id'], name=op.f('fk_asset_produced_by_run_id_build_run'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_asset'))
    )
    op.create_index(op.f('ix_asset_asset_type'), 'asset', ['asset_type'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_asset_asset_type'), table_name='asset')
    op.drop_table('asset')
