"""deploy_config, deployment (deploys of a package to an environment)

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-08 10:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0021'
down_revision: Union[str, None] = '0020'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('deploy_config',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(length=60), nullable=False),
    sa.Column('driver', sa.String(length=40), nullable=False),
    sa.Column('description', sa.String(length=300), nullable=False),
    sa.Column('config_json', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_deploy_config')),
    sa.UniqueConstraint('key', name=op.f('uq_deploy_config_key'))
    )
    op.create_table('deployment',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('package_id', sa.Integer(), nullable=True),
    sa.Column('package_tag', sa.String(length=40), nullable=False),
    sa.Column('deploy_config_id', sa.Integer(), nullable=False),
    sa.Column('classes_json', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('previous_package_id', sa.Integer(), nullable=True),
    sa.Column('rolled_back_from_id', sa.Integer(), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('triggered_by', sa.String(length=80), nullable=False),
    sa.Column('build_run_id', sa.Integer(), nullable=True),
    sa.Column('detail_json', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['package_id'], ['package.id'], name=op.f('fk_deployment_package_id_package'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['deploy_config_id'], ['deploy_config.id'], name=op.f('fk_deployment_deploy_config_id_deploy_config'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['previous_package_id'], ['package.id'], name=op.f('fk_deployment_previous_package_id_package'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['rolled_back_from_id'], ['deployment.id'], name=op.f('fk_deployment_rolled_back_from_id_deployment'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['build_run_id'], ['build_run.id'], name=op.f('fk_deployment_build_run_id_build_run'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_deployment'))
    )
    op.create_index(op.f('ix_deployment_package_id'), 'deployment', ['package_id'], unique=False)
    op.create_index(op.f('ix_deployment_deploy_config_id'), 'deployment', ['deploy_config_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_deployment_deploy_config_id'), table_name='deployment')
    op.drop_index(op.f('ix_deployment_package_id'), table_name='deployment')
    op.drop_table('deployment')
    op.drop_table('deploy_config')
