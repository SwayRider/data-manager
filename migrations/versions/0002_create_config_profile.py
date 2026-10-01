"""create config_profile

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-30 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('config_profile',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100, collation='NOCASE'), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_config_profile')),
    sa.UniqueConstraint('name', name=op.f('uq_config_profile_name'))
    )


def downgrade() -> None:
    op.drop_table('config_profile')
