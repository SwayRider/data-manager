"""build_run, build_step, download_record

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-01 10:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0015'
down_revision: Union[str, None] = '0014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('build_run',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('stage_key', sa.String(length=50), nullable=False),
    sa.Column('config_profile_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('params_json', sa.JSON(), nullable=False),
    sa.Column('input_versions_json', sa.JSON(), nullable=False),
    sa.Column('report_json', sa.JSON(), nullable=True),
    sa.Column('rq_job_id', sa.String(length=80), nullable=True),
    sa.Column('triggered_by', sa.String(length=80), nullable=False),
    sa.Column('error_type', sa.String(length=80), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('review_note', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['config_profile_id'], ['config_profile.id'], name=op.f('fk_build_run_config_profile_id_config_profile'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_build_run'))
    )
    op.create_table('build_step',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('sequence_index', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('progress_current', sa.Integer(), nullable=True),
    sa.Column('progress_total', sa.Integer(), nullable=True),
    sa.Column('progress_message', sa.String(length=300), nullable=True),
    sa.Column('error_type', sa.String(length=80), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['run_id'], ['build_run.id'], name=op.f('fk_build_step_run_id_build_run'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_build_step'))
    )
    op.create_table('download_record',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('source_key', sa.String(length=200), nullable=False),
    sa.Column('version_label', sa.String(length=20), nullable=False),
    sa.Column('url', sa.String(length=500), nullable=False),
    sa.Column('filename', sa.String(length=200), nullable=False),
    sa.Column('local_path', sa.String(length=500), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('etag', sa.String(length=200), nullable=True),
    sa.Column('upstream_modified', sa.String(length=100), nullable=True),
    sa.Column('fetched_at', sa.DateTime(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('pinned', sa.Boolean(), nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['run_id'], ['build_run.id'], name=op.f('fk_download_record_run_id_build_run'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_download_record')),
    sa.UniqueConstraint('source_key', 'version_label', name=op.f('uq_download_record_source_key'))
    )
    op.create_index(op.f('ix_download_record_source_key'), 'download_record', ['source_key'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_download_record_source_key'), table_name='download_record')
    op.drop_table('download_record')
    op.drop_table('build_step')
    op.drop_table('build_run')
