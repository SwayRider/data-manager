"""region_overlap: store auto-detected overlap too (replaces region_overlap_override)

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-30 20:00:00

Existing override rows are kept (modes include/exclude). Auto-detected rows are not
created here (needs geometry); regions with `overlap_evaluated_at` NULL are evaluated
on first read, or with `flask evaluate-overlap`.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0008'
down_revision: Union[str, None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('region_overlap',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('region_id', sa.Integer(), nullable=False),
    sa.Column('country_iso', sa.String(length=10), nullable=False),
    sa.Column('mode', sa.String(length=10), nullable=False),
    sa.ForeignKeyConstraint(['country_iso'], ['country.iso2'], name=op.f('fk_region_overlap_country_iso_country'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['region_id'], ['region.id'], name=op.f('fk_region_overlap_region_id_region'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_region_overlap')),
    sa.UniqueConstraint('region_id', 'country_iso', name=op.f('uq_region_overlap_region_id'))
    )
    op.execute("INSERT INTO region_overlap (region_id, country_iso, mode) "
               "SELECT region_id, country_iso, mode FROM region_overlap_override")
    op.drop_table('region_overlap_override')
    with op.batch_alter_table('region') as batch_op:
        batch_op.add_column(sa.Column('overlap_evaluated_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('region') as batch_op:
        batch_op.drop_column('overlap_evaluated_at')
    op.create_table('region_overlap_override',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('region_id', sa.Integer(), nullable=False),
    sa.Column('country_iso', sa.String(length=10), nullable=False),
    sa.Column('mode', sa.String(length=10), nullable=False),
    sa.ForeignKeyConstraint(['country_iso'], ['country.iso2'], name=op.f('fk_region_overlap_override_country_iso_country'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['region_id'], ['region.id'], name=op.f('fk_region_overlap_override_region_id_region'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_region_overlap_override')),
    sa.UniqueConstraint('region_id', 'country_iso', name=op.f('uq_region_overlap_override_region_id'))
    )
    op.execute("INSERT INTO region_overlap_override (region_id, country_iso, mode) "
               "SELECT region_id, country_iso, mode FROM region_overlap WHERE mode IN ('include','exclude')")
    op.drop_table('region_overlap')
