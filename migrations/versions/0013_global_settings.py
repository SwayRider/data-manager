"""global_settings

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-30 23:59:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0013'
down_revision: Union[str, None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('global_setting',
    sa.Column('key', sa.String(length=80), nullable=False),
    sa.Column('value_json', sa.JSON(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('key', name=op.f('pk_global_setting'))
    )


def downgrade() -> None:
    op.drop_table('global_setting')
