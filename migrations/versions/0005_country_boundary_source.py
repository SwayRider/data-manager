"""country_boundary_source

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-30 17:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0005'
down_revision: Union[str, None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('country_boundary_source',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('country_iso', sa.String(length=10), nullable=False),
    sa.Column('source_key', sa.String(length=50), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('config_json', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['country_iso'], ['country.iso2'], name=op.f('fk_country_boundary_source_country_iso_country'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_country_boundary_source')),
    sa.UniqueConstraint('country_iso', 'source_key', name=op.f('uq_country_boundary_source_country_iso'))
    )


def downgrade() -> None:
    op.drop_table('country_boundary_source')
