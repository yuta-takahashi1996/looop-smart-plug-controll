"""add FOSSiBOT F2400 to device_types（docs/17 §17.9.4、2026-11-15 に案 B で採用）

Revision ID: 9a1c4e7b2f30
Revises: f5cea316df10
Create Date: 2026-11-15 22:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9a1c4e7b2f30'
down_revision: Union[str, Sequence[str], None] = 'f5cea316df10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

device_types = sa.table(
    'device_types',
    sa.column('vendor', sa.String),
    sa.column('code', sa.String),
    sa.column('name', sa.String),
    sa.column('category', sa.String),
    sa.column('power_control', sa.Boolean),
    sa.column('charge_mode', sa.Boolean),
    sa.column('default_capacity_wh', sa.Float),
    sa.column('source', sa.String),
    sa.column('note', sa.Text),
)


def upgrade() -> None:
    """Upgrade schema."""
    exists = op.get_bind().execute(
        sa.select(sa.func.count()).select_from(device_types)
        .where(device_types.c.vendor == 'FOSSIBOT', device_types.c.code == 'FOSSIBOT_F2400')
    ).scalar()
    if not exists:  # 起動時の補完（seed_device_types）で登録済みの場合がある
        op.bulk_insert(device_types, [{
            'vendor': 'FOSSIBOT', 'code': 'FOSSIBOT_F2400', 'name': 'FOSSiBOT F2400', 'category': 'PORTABLE_POWER',
            'power_control': False, 'charge_mode': True, 'default_capacity_wh': 2048.0, 'source': 'CUSTOM', 'note': None,
        }])


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(device_types.delete().where(device_types.c.vendor == 'FOSSIBOT', device_types.c.code == 'FOSSIBOT_F2400'))
