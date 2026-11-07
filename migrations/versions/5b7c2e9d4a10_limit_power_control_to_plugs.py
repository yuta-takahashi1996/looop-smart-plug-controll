"""limit power control to plugs（docs/17 §17.4.2、2026-11-07 にリレースイッチを非対応へ変更）

先の 3fbf63252a5d を適用済みの DB で、リレースイッチを非対応にする（新規の DB では初期登録の時点で非対応）。

Revision ID: 5b7c2e9d4a10
Revises: 3fbf63252a5d
Create Date: 2026-11-07 14:10:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5b7c2e9d4a10'
down_revision: Union[str, Sequence[str], None] = '3fbf63252a5d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RELAY_CODES = ('Relay Switch 1', 'Relay Switch 1PM')
RELAY_NOTE = '実機で ON／OFF と状態の照合を確認してから対応する'

device_types = sa.table(
    'device_types',
    sa.column('vendor', sa.String),
    sa.column('code', sa.String),
    sa.column('power_control', sa.Boolean),
    sa.column('note', sa.Text),
)


def _set(power_control: bool, note: str | None) -> None:
    op.execute(
        device_types.update()
        .where(device_types.c.vendor == 'SWITCHBOT', device_types.c.code.in_(RELAY_CODES))
        .values(power_control=power_control, note=note)
    )


def upgrade() -> None:
    """Upgrade schema."""
    _set(False, RELAY_NOTE)


def downgrade() -> None:
    """Downgrade schema."""
    _set(True, None)
