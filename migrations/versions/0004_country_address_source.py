"""country_address_source; move country.openaddresses_files_json into it

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-30 15:00:00

"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0004'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

source_table = sa.table(
    'country_address_source',
    sa.column('country_iso', sa.String),
    sa.column('source_key', sa.String),
    sa.column('enabled', sa.Boolean),
    sa.column('config_json', sa.JSON),
)


def upgrade() -> None:
    op.create_table('country_address_source',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('country_iso', sa.String(length=10), nullable=False),
    sa.Column('source_key', sa.String(length=50), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('config_json', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['country_iso'], ['country.iso2'], name=op.f('fk_country_address_source_country_iso_country'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_country_address_source')),
    sa.UniqueConstraint('country_iso', 'source_key', name=op.f('uq_country_address_source_country_iso'))
    )

    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT iso2, openaddresses_files_json FROM country")).fetchall()
    for iso2, files_json in rows:
        files = json.loads(files_json) if files_json else []
        bind.execute(sa.insert(source_table).values(
            country_iso=iso2, source_key='openaddresses', enabled=bool(files), config_json={'files': files}))

    with op.batch_alter_table('country') as batch_op:
        batch_op.drop_column('openaddresses_files_json')


def downgrade() -> None:
    with op.batch_alter_table('country') as batch_op:
        batch_op.add_column(sa.Column('openaddresses_files_json', sa.JSON(), nullable=False, server_default='[]'))

    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT country_iso, config_json FROM country_address_source WHERE source_key = 'openaddresses'")).fetchall()
    for iso2, config_json in rows:
        files = (json.loads(config_json) if config_json else {}).get('files', [])
        bind.execute(sa.text("UPDATE country SET openaddresses_files_json = :f WHERE iso2 = :i"),
                     {'f': json.dumps(files), 'i': iso2})

    op.drop_table('country_address_source')
