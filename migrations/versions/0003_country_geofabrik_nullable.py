"""country.geofabrik_path nullable (catalog now seeds all countries)

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-30 13:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0003'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('country') as batch_op:
        batch_op.alter_column('geofabrik_path', existing_type=sa.String(length=200), nullable=True)


def downgrade() -> None:
    op.execute("DELETE FROM country WHERE geofabrik_path IS NULL")
    with op.batch_alter_table('country') as batch_op:
        batch_op.alter_column('geofabrik_path', existing_type=sa.String(length=200), nullable=False)
