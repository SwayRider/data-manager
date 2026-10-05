"""package, package_item, package_label (the package repository index)

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-05 20:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0019'
down_revision: Union[str, None] = '0018'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('package',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tag', sa.String(length=40), nullable=False),
    sa.Column('config_profile_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('path', sa.String(length=500), nullable=False),
    sa.Column('note', sa.String(length=500), nullable=False),
    sa.Column('protected', sa.Boolean(), nullable=False),
    sa.Column('created_by', sa.String(length=80), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('build_run_id', sa.Integer(), nullable=True),
    sa.Column('package_json_hash', sa.String(length=64), nullable=True),
    sa.ForeignKeyConstraint(['config_profile_id'], ['config_profile.id'], name=op.f('fk_package_config_profile_id_config_profile'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['build_run_id'], ['build_run.id'], name=op.f('fk_package_build_run_id_build_run'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_package')),
    sa.UniqueConstraint('tag', name=op.f('uq_package_tag'))
    )
    op.create_table('package_item',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('package_id', sa.Integer(), nullable=False),
    sa.Column('class_', sa.String(length=20), nullable=False),
    sa.Column('region', sa.String(length=100), nullable=True),
    sa.Column('path', sa.String(length=500), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('asset_id', sa.Integer(), nullable=True),
    sa.Column('download_id', sa.Integer(), nullable=True),
    sa.Column('meta_json', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['package_id'], ['package.id'], name=op.f('fk_package_item_package_id_package'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_package_item'))
    )
    op.create_index(op.f('ix_package_item_package_id'), 'package_item', ['package_id'], unique=False)
    op.create_table('package_label',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('package_id', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(length=100), nullable=False),
    sa.Column('value', sa.String(length=300), nullable=False),
    sa.Column('origin', sa.String(length=10), nullable=False),
    sa.ForeignKeyConstraint(['package_id'], ['package.id'], name=op.f('fk_package_label_package_id_package'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_package_label'))
    )
    op.create_index(op.f('ix_package_label_package_id'), 'package_label', ['package_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_package_label_package_id'), table_name='package_label')
    op.drop_table('package_label')
    op.drop_index(op.f('ix_package_item_package_id'), table_name='package_item')
    op.drop_table('package_item')
    op.drop_table('package')
