"""download_record.data_timestamp

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-01 15:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0017'
down_revision: Union[str, None] = '0016'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('download_record') as batch_op:
        batch_op.add_column(sa.Column('data_timestamp', sa.String(length=40), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('download_record') as batch_op:
        batch_op.drop_column('data_timestamp')
