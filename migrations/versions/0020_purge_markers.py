"""purge markers on download_record, verified_at on package (post-packaging cleanup)

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-05 22:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0020'
down_revision: Union[str, None] = '0019'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('download_record') as batch:
        batch.add_column(sa.Column('purged_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('purged_package', sa.String(length=40), nullable=True))
    with op.batch_alter_table('package') as batch:
        batch.add_column(sa.Column('verified_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('package') as batch:
        batch.drop_column('verified_at')
    with op.batch_alter_table('download_record') as batch:
        batch.drop_column('purged_package')
        batch.drop_column('purged_at')
